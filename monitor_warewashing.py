#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WW Competitor Monitor
- Crawl seeds from config/urls.yaml
- Discover/Archive PDFs and product pages
- Deterministic product-line inference with Champion overrides
- Post-crawl reports (rules -> ML fallback) and Graph email with attachments
"""

import os
import re
import sys
import sqlite3
import hashlib
import json
import time
import base64
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

# -------------------
# Optional: post-crawl classifier & reporting
# -------------------
# These imports are optional at runtime; we guard-call them below.
try:
    from classifier.rules import run_rules_only, OUTPUT_DIR as CLASS_OUT_DIR
    from classifier.ml_fallback import apply_fallback
    from reporting.build_reports import build_pivot
    _CLASSIFIER_AVAILABLE = True
except Exception:
    _CLASSIFIER_AVAILABLE = False

# -------------------
# Lightweight logging
# -------------------
def dbg(msg: str):
    if os.getenv("DEBUG_LOG", "0") == "1":
        print(msg)

# -------------------
# Config loading (YAML) and base paths
# -------------------
def load_yaml(path):
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

BASE_DIR = os.path.dirname(__file__)
CONFIG_DIR = os.path.join(BASE_DIR, "config")

COMP_CONF = load_yaml(os.path.join(CONFIG_DIR, "competitors.yaml")) if os.path.exists(os.path.join(CONFIG_DIR, "competitors.yaml")) else {}
URLS_CONF = load_yaml(os.path.join(CONFIG_DIR, "urls.yaml"))        if os.path.exists(os.path.join(CONFIG_DIR, "urls.yaml"))        else {}
STYLE_CONF = load_yaml(os.path.join(CONFIG_DIR, "styling.yaml"))    if os.path.exists(os.path.join(CONFIG_DIR, "styling.yaml"))     else {}

# Per-site rules (optional)
RULES_PATH = os.path.join(CONFIG_DIR, "site_rules.yaml")
SITE_RULES = load_yaml(RULES_PATH) if os.path.exists(RULES_PATH) else {}

# Safe fallbacks
DEFAULT_COMPETITORS = [
    "Champion", "Jackson", "Meiko", "CMA", "Noble",
    "ADS", "Moyer Diebel", "Douglas", "LVO"
]
DEFAULT_LINES = [
    "Door Type", "Undercounter", "Prep Washer",
    "Rack Conveyor", "Flight Type"
]
COMPETITOR_COLS = COMP_CONF.get("competitors", DEFAULT_COMPETITORS)
LINES_ORDER = COMP_CONF.get("lines", DEFAULT_LINES)

DOOR, UNDER, PREP, RACK, FLIGHT = (
    "Door Type", "Undercounter", "Prep Washer", "Rack Conveyor", "Flight Type"
)

# -------------------
# Styling defaults
# -------------------
EMAIL_STYLE = STYLE_CONF.get("email", {}) if STYLE_CONF else {}
EMAIL_FONT_FAMILY = EMAIL_STYLE.get("font_family", "Segoe UI, Arial, sans-serif")
EMAIL_FONT_SIZE   = EMAIL_STYLE.get("font_size", "14px")
EMAIL_HEADER_BG   = EMAIL_STYLE.get("header_bg", "#f3f3f3")
EMAIL_HEADER_FG   = EMAIL_STYLE.get("header_fg", "#000000")
EMAIL_BODY_BG     = EMAIL_STYLE.get("body_bg", "#ffffff")
EMAIL_BORDER_CLR  = EMAIL_STYLE.get("border_color", "#dddddd")
EMAIL_CELL_PAD    = EMAIL_STYLE.get("cell_padding", "6")
EMAIL_COL_WIDTH   = EMAIL_STYLE.get("column_width", "180px")
EMAIL_UPDATE_BG   = EMAIL_STYLE.get("update_bg", "#FFFFE0")

# -------------------
# Archiving configuration
# -------------------
ARCHIVE_DIR = os.getenv("ARCHIVE_DIR", "archive")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_REF_NAME   = os.getenv("GITHUB_REF_NAME", "main")
BOOTSTRAP         = os.getenv("BOOTSTRAP_ARCHIVE") == "1"

# -------------------
# Utilities
# -------------------
def build_na_map():
    na = {line: {c: False for c in COMPETITOR_COLS} for line in LINES_ORDER}
    for comp in COMPETITOR_COLS:
        comp_lines = URLS_CONF.get(comp, {}) if URLS_CONF else {}
        for line in LINES_ORDER:
            urls = comp_lines.get(line, None)
            if isinstance(urls, list) and len(urls) == 0:
                na[line][comp] = True
    return na

def ensure_archive_tree():
    """Pre-create archive/<Competitor>/<Line> folders for a stable layout."""
    for competitor in (URLS_CONF or {}).keys():
        for line in LINES_ORDER:
            subdir = os.path.join(
                ARCHIVE_DIR,
                re.sub(r"[\\/]+", "_", competitor).strip(),
                re.sub(r"[\\/]+", "_", line).strip()
            )
            os.makedirs(subdir, exist_ok=True)

# -------------------
# HTML/link helpers
# -------------------
def a(href: str, label: str) -> str:
    href = href or "#"
    label = label or (href if href != "#" else "link")
    return f'<a href="{href}">{label}</a>'


def display_url_label(href: str, max_len: int = 60) -> str:
    try:
        u = urlparse(href)
        path = (u.path or "").rstrip("/")
        last = path.split("/")[-1] if path else ""
        label = f"{u.netloc}/{last}" if last else (u.netloc or href)
        label = unquote(label)
        label = re.sub(r"[_\-]+", " ", label).strip()
        if len(label) > max_len:
            keep = max_len // 2 - 1
            label = f"{label[:keep]}…{label[-keep:]}"
        return label or href
    except Exception:
        return href

ACRONYM_KEEP = {"PRO", "VHR", "ER", "HT", "LT", "HR"}

def beautify_filename(url_or_name: str) -> str:
    name = unquote(url_or_name.split("?")[0].split("#")[0].split("/")[-1])
    name = re.sub(r"\.pdf$", "", name, flags=re.I)
    name = re.sub(r"[_\-]+", " ", name)

    # Normalize common doc terms
    name = re.sub(r"\b(spec(?:\.?ification)?\s*sheet|specsheet)\b", "Spec Sheet", name, flags=re.I)
    name = re.sub(r"\b(data\s*sheet|datasheet|product\s*data|technical\s*data|tech\s*data)\b", "Data Sheet", name, flags=re.I)
    name = re.sub(r"\b(brochure|sales\s*sheet|sell\s*sheet|flyer)\b", "Brochure", name, flags=re.I)

    name = re.sub(r"\s{2,}", " ", name).strip()

    words = []
    for w in name.split(" "):
        if w.isupper() and len(w) <= 5:
            words.append(w)
        elif w.upper() in ACRONYM_KEEP:
            words.append(w.upper())
        else:
            words.append(w.capitalize())
    return " ".join(words)

def extract_embedded_links(html: str, base: str) -> set[str]:
    """Find product links present in onclick/data attributes/JS helpers."""
    out = set()
    # onclick="location.href='...'" or onclick="window.location='...'"
    for m in re.finditer(r'onclick\s*=\s*"(?:location\.href|window\.location)\s*=\s*[\'"]([^\'"]+)[\'"]', html, re.I):
        out.add(urljoin(base, m.group(1)))
    for m in re.finditer(r"onclick\s*=\s*'(?:location\.href|window\.location)\s*=\s*[\"']([^\"']+)[\"']", html, re.I):
        out.add(urljoin(base, m.group(1)))
    # data-url="/path/to/product/"
    for m in re.finditer(r'data-url\s*=\s*"([^"]+)"', html, re.I):
        out.add(urljoin(base, m.group(1)))
    for m in re.finditer(r"data-url\s*=\s*'([^']+)'", html, re.I):
        out.add(urljoin(base, m.group(1)))
    # JS helpers like goToProduct('/path/...') or openProduct("...")
    for m in re.finditer(r'(?:goToProduct|openProduct)\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)', html, re.I):
        out.add(urljoin(base, m.group(1)))
    return out

def extract_tile_links(html: str, base_url: str, competitor: str) -> list[tuple[str, str]]:
    """
    Return (href, title) pairs for product tiles/cards.
    Uses vendor selectors from site_rules.yaml, else a generic fallback.
    """
    out = []
    soup = BeautifulSoup(html, "lxml")
    selectors = _get_tile_selectors(competitor)

    def add(a):
        try:
            href = a.get("href")
            if not href:
                return
            href = urljoin(base_url, href)
            title = a.get_text(" ", strip=True) or href
            if len(title) >= 3:
                out.append((href, title))
        except Exception:
            pass

    if selectors:
        for sel in selectors:
            for a in soup.select(sel):
                add(a)
    else:
        # Fallback: typical product grids (works on many WP/e‑comm sites)
        for container in soup.select(".products, .product-grid, .cards, .grid, .row"):
            for a in container.select("a"):
                add(a)

    # de‑dupe
    seen, uniq = set(), []
    for href, title in out:
        key = (href, title.lower())
        if key not in seen:
            uniq.append((href, title))
            seen.add(key)
    return uniq

# -------------------
# PDF patterns & classification
# -------------------
PDF_PATTERNS = re.compile(
    r"(spec(?:ification)?[\s\-]?sheet|specsheet|"
    r"data[\s\-]?sheet|datasheet|product[\s\-]?data|technical[\s\-]?data|tech[\s\-]?data|"
    r"cut[\s\-]?sheet|cutsheet|"
    r"brochure|flyer|sales[\s\-]?sheet|sell[\s\-]?sheet)",
    re.I
)

def classify_pdf(text_or_url: str):
    s = text_or_url or ""
    if re.search(r"(spec(?:ification)?[\s\-]?sheet|specsheet|cut[\s\-]?sheet|cutsheet)", s, re.I):
        return "Spec Sheet"
    if re.search(r"(data[\s\-]?sheet|datasheet|product[\s\-]?data|technical[\s\-]?data|tech[\s\-]?data)", s, re.I):
        return "Data Sheet"
    if re.search(r"(brochure|sales[\s\-]?sheet|sell[\s\-]?sheet|flyer|product[\s\-]?sheet)", s, re.I):
        return "Brochure"
    return None

# -------------------
# DB helpers
# -------------------
DB_PATH = os.getenv("STATE_DB", "state.db")


def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # Existing tables (unchanged)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS resources(
      url TEXT PRIMARY KEY,
      competitor TEXT,
      line TEXT,
      kind TEXT,
      last_modified TEXT,
      etag TEXT,
      hash TEXT,
      title TEXT,
      first_seen TEXT,
      last_seen TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS events(
      ts TEXT,
      competitor TEXT,
      line TEXT,
      url TEXT,
      what TEXT,
      change TEXT,
      archived_path TEXT,
      archived_url TEXT
    )
    """)

def tiles_get_previous(cur, competitor: str, line: str, page_url: str) -> dict[str, str]:
    cur.execute("""
        SELECT tile_href, tile_title
        FROM catalog_tiles
        WHERE competitor=? AND line=? AND page_url=?
    """, (competitor, line, page_url))
    return {row[0]: row[1] for row in cur.fetchall()}

def tiles_upsert(cur, competitor: str, line: str, page_url: str, href: str, title: str):
    now = datetime.now(timezone.utc).isoformat()
    cur.execute("""
      INSERT INTO catalog_tiles(competitor, line, page_url, tile_title, tile_href, first_seen, last_seen)
      VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(competitor, line, page_url, tile_href)
      DO UPDATE SET tile_title=excluded.tile_title, last_seen=excluded.last_seen
    """, (competitor, line, page_url, title, href, now, now))

    # --------------------------------------------------------
    # NEW: store product tiles found on category/product pages
    # --------------------------------------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS catalog_tiles(
      competitor TEXT,
      line       TEXT,
      page_url   TEXT,
      tile_title TEXT,
      tile_href  TEXT,
      first_seen TEXT,
      last_seen  TEXT,
      PRIMARY KEY (competitor, line, page_url, tile_href)
    )
    """)

    # -----------------------------------------------
    # OPTIONAL: add 'title' column on events (one-time)
    # lets us persist tile titles for email bullets
    # -----------------------------------------------
    try:
        cur.execute("ALTER TABLE events ADD COLUMN title TEXT")
    except Exception:
        # Column probably exists already—ignore
        pass

    con.commit()
    return con

def get_existing_resource(cur, url):
    cur.execute("""
    SELECT url, competitor, line, kind, last_modified, etag, hash, title
    FROM resources WHERE url=?
    """, (url,))
    row = cur.fetchone()
    if row:
        keys = ["url", "competitor", "line", "kind", "last_modified", "etag", "hash", "title"]
        return dict(zip(keys, row))
    return None

def record_resource(cur, url, competitor, line, kind, headers, content_hash, title):
    prev = get_existing_resource(cur, url)
    cur.execute("SELECT last_modified, etag, hash FROM resources WHERE url=?", (url,))
    row = cur.fetchone()

    now = datetime.now(timezone.utc).isoformat()
    last_mod = headers.get("Last-Modified") if headers else None
    etag     = headers.get("ETag") if headers else None

    if row is None:
        cur.execute("""
        INSERT INTO resources(url, competitor, line, kind, last_modified, etag, hash, title, first_seen, last_seen)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (url, competitor, line, kind, last_mod, etag, content_hash, title, now, now))
        return "added", prev

    prev_mod, prev_etag, prev_hash = row
    changed = False
    if (last_mod and last_mod != prev_mod) or (etag and etag != prev_etag):
        changed = True
    if content_hash and prev_hash and content_hash != prev_hash:
        changed = True

    if changed:
        cur.execute("""
        UPDATE resources
        SET last_modified=?, etag=?, hash=?, title=?, last_seen=?, line=?
        WHERE url=?
        """, (last_mod, etag, content_hash or prev_hash, title, now, line, url))
        return "updated", prev

    cur.execute("UPDATE resources SET last_seen=? WHERE url=?", (now, url))
    return None, prev

# -------------------
# HTTP helpers
# -------------------
REQUEST_TIMEOUT = 25
DEFAULT_UA = "WW-Competitor-Monitor/1.0 (+market intel; contact: ww-monitor@itwfeg.com)"
session = requests.Session()
session.headers.update({"User-Agent": os.getenv("UA_OVERRIDE", DEFAULT_UA)})

def safe_request(method, url):
    try:
        r = session.request(method, url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        return r
    except Exception as e:
        dbg(f"[HTTP-{method}] {url} -> {e}")
        return None

def sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()

def get_html(url):
    r = safe_request("GET", url)
    if not r: return None, None
    return r.text, r.headers

def head(url):
    r = safe_request("HEAD", url)
    return r.headers if (r and r.status_code < 400) else None

def get_pdf_hash(url):
    r = safe_request("GET", url)
    if not r: return None, None
    return sha256_bytes(r.content), r.headers

def extract_links(html, base):
    soup = BeautifulSoup(html, "lxml")
    links = []
    title = (soup.title.string.strip() if soup.title and soup.title.string else "")
    for a_tag in soup.find_all("a", href=True):
        href = urljoin(base, a_tag["href"])
        text = (a_tag.get_text(" ", strip=True) or "")
        links.append((href, text))
    return title, links

def is_pdf(url):
    return url.lower().split("?")[0].endswith(".pdf")

def strip_main_text(html):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["nav", "header", "footer", "script", "style", "noscript", "svg"]):
        tag.decompose()
    txt = soup.get_text(" ", strip=True)
    return " ".join(txt.split())[:20000]

# -------------------
# Archiving helpers
# -------------------
def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def archive_pdf(competitor: str, line: str, url: str, content: bytes, display_name: str, sha_hex: str) -> str:
    """
    Save the PDF under archive/<competitor>/<line>/YYYY-MM-DD__NAME__sha256_xxxxxxxx.pdf
    Returns the local repo path to the archived PDF.
    """
    safe_comp = re.sub(r"[\\/]+", "_", competitor).strip()
    safe_line = re.sub(r"[\\/]+", "_", line).strip()
    subdir = os.path.join(ARCHIVE_DIR, safe_comp, safe_line)
    ensure_dir(subdir)
    date_str = datetime.now().strftime("%Y-%m-%d")
    base_name = re.sub(r"[^\w\- \.\(\)]+", "_", display_name).strip()
    fname = f"{date_str}__{base_name}__sha256_{sha_hex[:8]}.pdf"
    fpath = os.path.join(subdir, fname)
    with open(fpath, "wb") as f:
        f.write(content)
    return fpath

def build_github_raw_url(repo: str, branch: str, local_path: str) -> str:
    local_path = local_path.replace("\\", "/")
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{local_path}"

# -------------------
# Site rules (generic) + line inference
# -------------------
def _compile_patterns(patts):
    if not patts:
        return []
    if isinstance(patts, (list, tuple)):
        return [re.compile(p, re.I) for p in patts]
    return [re.compile(patts, re.I)]

def _get_rules_for(competitor: str):
    r = SITE_RULES.get(competitor, {}) or {}
    return {
        "host_allow": _compile_patterns(r.get("host_allow")),
        "path_block": _compile_patterns(r.get("path_block")),
        "page_allow": _compile_patterns(r.get("page_allow")),
        "pdf_host_allow": _compile_patterns(r.get("pdf_host_allow")),
        "line_patterns": {k: _compile_patterns(v) for k, v in (r.get("line_patterns") or {}).items()},
        "pdf_path_line_hints": {k: re.compile(v, re.I) for k, v in (r.get("pdf_path_line_hints") or {}).items()},
        "pdf_accept_all": bool(r.get("pdf_accept_all", False)),
    }


def _get_tile_selectors(competitor: str) -> list[str]:
    """
    Optional CSS selectors from site_rules.yaml for product tiles/cards.
    """
    try:
        rules = SITE_RULES.get(competitor, {}) or {}
        sels = rules.get("tile_selectors") or []
        if isinstance(sels, str):
            sels = [sels]
        return [s for s in sels if isinstance(s, str) and s.strip()]
    except Exception:
        return []

def _host_allowed(host_allow, netloc: str) -> bool:
    return (not host_allow) or any(p.search(netloc) for p in host_allow)

def _path_blocked(path_block, path: str) -> bool:
    return any(p.search(path) for p in path_block)

def _page_allowed(page_allow, path: str) -> bool:
    return any(p.search(path) for p in page_allow)

def looks_like_product_page(url: str, competitor: str) -> bool:
    """Rules-aware product-page decision."""
    u = urlparse(url)
    path = u.path or "/"

    # Quick rejects
    low = url.lower()
    if any(x in low for x in ("/privacy", "/terms", "/sitemap", "/contact", "/search", "/careers")):
        return False

    rules = _get_rules_for(competitor)
    if rules["path_block"] and _path_blocked(rules["path_block"], path):
        return False
    if rules["page_allow"]:
        if _page_allowed(rules["page_allow"], path):
            return True

    # Fallback heuristic
    return any(x in low for x in ("/product", "/products/", "/our-products", "/rack", "/door", "/flight", "/dish", "/washer", "/categories/"))

# ---- Deterministic precedence rules for product-line (global) ----
# Flight > Rack > Door > Undercounter > Prep Washer > Other

_RX_FLIGHT = [
    re.compile(r"\b(EUCCW?|EUCC)\b", re.I),              # EUCC/EUCCW are Flight
    re.compile(r"\bPRO\s*Flight\b", re.I),               # PRO Flight Series
    re.compile(r"\bE\s*Series\s*Flight\b", re.I),        # E Series Flight
    re.compile(r"\bFlight\s*Machine\b", re.I),
    re.compile(r"\bFlight\s*Type\b", re.I),
]

# ✔ PRO-number families belong to Rack Conveyor (44/54/64/66/76/80/86/90)
#   These MUST be checked BEFORE Door/Undercounter.

_RX_RACK = [
    # PRO-number families (most reliable)
    re.compile(r"\b(44|54|64|66|76|80|86|90)\s*PRO\b", re.I),    
    re.compile(r"\b(44|54|64|66|76|80|86|90)\s*PRO\b.*", re.I),  # allow trailing text like FF/HR/Steam/VHR/HD

    # Also catch variants like “PRO 90B Loader/Unloader”
    re.compile(r"\bPRO\s*90B\b", re.I),

    # As fallback only (keep below)
    re.compile(r"rack\s*conveyor", re.I),
]


# ✔ Classic Door Type families
_RX_DOOR = [
    re.compile(r"\b(DH|DL)\s*\d", re.I),
    re.compile(r"\bHood\s*Type\b", re.I),
    re.compile(r"\bTall\s*Hood\b", re.I),
    re.compile(r"\bDoor\s*Type\b", re.I),
]

# ✔ Undercounter (UH/UL/Glasswasher/CG/UCC)
_RX_UNDER = [
    re.compile(r"\bGlasswasher(s)?\b", re.I),
    re.compile(r"\bCG[0-9 ]*\b", re.I),
    re.compile(r"\bU(H|L|HM|HB)\s*\d{2,4}[A-Z]?\b", re.I),
    re.compile(r"\bUCC(W)?\b", re.I),
    re.compile(r"\bUndercounter\b", re.I),
]

# ✔ Pot & Pan / Prep Washer
_RX_PP = [
    re.compile(r"\bPP\b", re.I),
    re.compile(r"\bPP\s*\d+\b", re.I),
    re.compile(r"\bPot\s*&?\s*Pan\b", re.I),
    re.compile(r"\bP524\b", re.I),
]

_RX_OTHER = [
    re.compile(r"waste\s*handling", re.I),
    re.compile(r"dehydrator", re.I),
    re.compile(r"\bCSS\b", re.I),
    re.compile(r"\bMRA\b", re.I),
]

def infer_line_global(text: str):
    # 1) Rack Conveyor first
    for rx in _RX_RACK:
        if rx.search(text):
            return "Rack Conveyor", 0.95

    # 2) Flight Type
    for rx in _RX_FLIGHT:
        if rx.search(text):
            return "Flight Type", 0.90

    # 3) Door Type
    for rx in _RX_DOOR:
        if rx.search(text):
            return "Door Type", 0.90

    # 4) Undercounter
    for rx in _RX_UNDER:
        if rx.search(text):
            return "Undercounter", 0.90

    # 5) Prep Washer
    for rx in _RX_PP:
        if rx.search(text):
            return "Prep Washer", 0.90

    # 6) Other LAST
    for rx in _RX_OTHER:
        if rx.search(text):
            return "Other", 0.95

    return "Other", 0.50

def infer_line_via_rules(url: str, anchor_text: str, page_text: str, competitor: str) -> tuple[str, float]:
    """
    Global deterministic precedence first (with your overrides),
    then per-site YAML rules (if present), otherwise Uncategorized.
    """
    text = " ".join([url or "", anchor_text or "", page_text or ""])

    # 1) Global deterministic (your instructions)
    line, conf = infer_line_global(text)
    if conf >= 0.90:
        return line, conf

    # 2) Per-site rules (optional)
    rules = _get_rules_for(competitor)
    # Strong hints from PDF path
    for line_key, rx in (rules["pdf_path_line_hints"] or {}).items():
        if rx.search(url):
            return line_key, 0.90

    # Pattern scores
    scores = {}
    for line_key, patt_list in (rules["line_patterns"] or {}).items():
        for p in patt_list:
            if p.search(text):
                scores[line_key] = scores.get(line_key, 0) + 1
    if scores:
        best = max(scores, key=scores.get)
        return best, min(0.95, 0.80 + 0.05 * scores[best])

    return "Other", 0.5

# -------------------
# Crawl logic
# -------------------
ABS_PDF_RX = re.compile(r'https?://[^"\'<>]+?\.pdf(?:\?[^"\'<>]*)?', re.I)
REL_PDF_RX = re.compile(r'(?:(?:\./|\../|/)[^"\'<>\s]+?\.pdf(?:\?[^"\'<>\s]*)?)', re.I)

def crawl_seed(cur, competitor, line, url):
    html, headers = get_html(url)
    results = []
    if not html:
        print(f"[CRAWL] NO HTML for {competitor} | {line} | {url}")
        return results

    title, links = extract_links(html, url)
    embedded = extract_embedded_links(html, url)
    if embedded:
        links.extend((e, "") for e in sorted(embedded))

    print(f"[CRAWL] {competitor} | {line} | {url} -> {len(links)} links")

    # Record the seed page
    content_hash = sha256_bytes(strip_main_text(html).encode("utf-8"))
    change, _ = record_resource(cur, url, competitor, line, "html", headers, content_hash, title)
    if change in ("added", "updated"):
        results.append({
            "competitor": competitor, "line": line, "url": url,
            "what": "Product page", "change": change,
            "old_url": None, "archived_path": None, "archived_url": None
        })

    # --- NEW: Tile extraction + diff on seed/category page ---
    tiles = extract_tile_links(html, url, competitor)
    if tiles:
        prev = tiles_get_previous(cur, competitor, line, url)
        seen_hrefs = set()

        for href, ttitle in tiles:
            seen_hrefs.add(href)
            tiles_upsert(cur, competitor, line, url, href, ttitle)

            # Classify the tile by its target (preferred), fallback to seed line
            tline, tconf = infer_line_via_rules(href, ttitle, "", competitor)
            use_line_for_tile = tline if tconf >= 0.70 else line

            if href not in prev:
                results.append({
                    "competitor": competitor,
                    "line": use_line_for_tile,
                    "url": href,
                    "title": ttitle,               # store title for bullets
                    "what": "Product tile",
                    "change": "added",
                    "old_url": None,
                    "archived_url": None,
                    "archived_path": None
                })

        # (Optional) report removed tiles:
        # for href in (set(prev.keys()) - seen_hrefs):
        #     results.append({
        #         "competitor": competitor,
        #         "line": line,
        #         "url": href,
        #         "title": prev.get(href, ""),
        #         "what": "Product tile",
        #         "change": "removed",
        #         "old_url": href,
        #         "archived_url": None,
        #         "archived_path": None
        #     })

    # Anchor text map
    link_text_map = {}
    for href, text in links:
        if href not in link_text_map:
            link_text_map[href] = text or ""

    # Discover PDFs in seed HTML + anchor href PDFs
    pdf_candidates = set()
    for m in ABS_PDF_RX.finditer(html):
        pdf_candidates.add(urljoin(url, m.group(0)))
    for m in REL_PDF_RX.finditer(html):
        pdf_candidates.add(urljoin(url, m.group(0)))
    for href, _ in links:
        if is_pdf(href):
            pdf_candidates.add(href)

    host = urlparse(url).netloc
    rules = _get_rules_for(competitor)

    # Restrict PDFs to allowed hosts (if specified)
    filtered_pdf_candidates = set()
    for href in pdf_candidates:
        h = urlparse(href).netloc
        if _host_allowed(rules["pdf_host_allow"], h):
            filtered_pdf_candidates.add(href)
    if filtered_pdf_candidates:
        pdf_candidates = filtered_pdf_candidates

    # Accept-all PDFs if configured (env or site rule)
    archive_all = os.getenv("ARCHIVE_ALL_PDFS", "0") == "1" or rules.get("pdf_accept_all", False)

    # ---- 1) Handle PDFs on seed page
    seen = set()
    for href in sorted(pdf_candidates):
        if href in seen:
            continue
        seen.add(href)

        text = link_text_map.get(href, "")
        if not archive_all and not (PDF_PATTERNS.search(href) or PDF_PATTERNS.search(text)):
            continue

        # HEAD → fallback GET for hash/headers
        h = head(href)
        dl_hash, dl_headers = (None, h)
        if h is None or not (h.get("ETag") or h.get("Last-Modified")):
            dl_hash, dl_headers = get_pdf_hash(href)

        # Classify doc type (fallback to Brochure if accept-all)
        doc_kind = classify_pdf(f"{href} {text}") or ("Brochure" if archive_all else None)
        if not doc_kind:
            continue

        # Infer product line via global rules + site rules
        inferred_line, conf = infer_line_via_rules(href, text, "", competitor)
        use_line = inferred_line if conf >= 0.60 else line

        change, prev_row = record_resource(cur, href, competitor, use_line, "pdf", dl_headers, dl_hash, text)
        if change in ("added", "updated"):
            # Download & archive
            pdf_resp = safe_request("GET", href)
            pdf_bytes = pdf_resp.content if pdf_resp else None
            if pdf_bytes is None:
                # retry once
                pdf_resp = safe_request("GET", href)
                pdf_bytes = pdf_resp.content if pdf_resp else None

            disp_name = beautify_filename(href or text or "document.pdf")
            sha_now = sha256_bytes(pdf_bytes) if pdf_bytes else (dl_hash or "nohash")
            archived_path = None
            archived_url  = None
            if pdf_bytes:
                archived_path = archive_pdf(competitor, use_line, href, pdf_bytes, disp_name, sha_now)
                if GITHUB_REPOSITORY:
                    archived_url = build_github_raw_url(GITHUB_REPOSITORY, GITHUB_REF_NAME, archived_path)

            print(f"[ARCHIVE] {competitor} | {use_line} | {disp_name} -> {archived_path or 'NO BYTES'}")
            results.append({
                "competitor": competitor,
                "line": use_line,
                "url": href,
                "what": doc_kind,
                "change": change,
                "old_url": prev_row["url"] if (prev_row and change == "updated") else None,
                "archived_url": archived_url,
                "archived_path": archived_path
            })

    # ---- 2) Crawl product pages (same host), one hop
    for href, text in links:
        u = urlparse(href)
        if u.netloc != host:
            continue
        if not looks_like_product_page(href, competitor):
            continue

        ph, ph_headers = get_html(href)
        if not ph:
            print(f"[CRAWL] NO HTML (subpage) for {href}")
            continue

        ptitle, sub_links = extract_links(ph, href)
        embedded_sub = extract_embedded_links(ph, href)
        if embedded_sub:
            sub_links.extend((e, "") for e in sorted(embedded_sub))

        page_text = strip_main_text(ph)

        # Infer line from URL + page text
        inferred_line, conf = infer_line_via_rules(href, text, page_text, competitor)
        use_line = inferred_line if conf >= 0.60 else line

        # Record the subpage itself
        content_hash = sha256_bytes(page_text.encode("utf-8"))
        change, _ = record_resource(cur, href, competitor, use_line, "html", ph_headers, content_hash, ptitle)
        if change in ("added", "updated"):
            results.append({
                "competitor": competitor, "line": use_line, "url": href,
                "what": "Product page", "change": change,
                "old_url": None, "archived_path": None, "archived_url": None
            })

        # Discover PDFs on the subpage
        sub_pdf_candidates = set()
        for m in ABS_PDF_RX.finditer(ph):
            sub_pdf_candidates.add(urljoin(href, m.group(0)))
        for m in REL_PDF_RX.finditer(ph):
            sub_pdf_candidates.add(urljoin(href, m.group(0)))
        for sub_href, _ in sub_links:
            if is_pdf(sub_href):
                sub_pdf_candidates.add(sub_href)

        # Apply allowed PDF hosts
        filtered_sub_pdf_candidates = set()
        for pdf_url in sub_pdf_candidates:
            if _host_allowed(rules["pdf_host_allow"], urlparse(pdf_url).netloc):
                filtered_sub_pdf_candidates.add(pdf_url)
        if filtered_sub_pdf_candidates:
            sub_pdf_candidates = filtered_sub_pdf_candidates

        for pdf_url in sorted(sub_pdf_candidates):
            sub_text = ""  # could parse nearby link text if needed

            if not archive_all and not (PDF_PATTERNS.search(pdf_url) or PDF_PATTERNS.search(sub_text)):
                continue

            h2 = head(pdf_url)
            dl_hash2, dl_headers2 = (None, h2)
            if h2 is None or not (h2.get("ETag") or h2.get("Last-Modified")):
                dl_hash2, dl_headers2 = get_pdf_hash(pdf_url)

            doc_kind2 = classify_pdf(f"{pdf_url} {sub_text}") or ("Brochure" if archive_all else None)
            if not doc_kind2:
                continue

            # Infer product line for archive folder
            page_line2, lconf2 = infer_line_via_rules(pdf_url, sub_text, "", competitor)
            use_line2 = page_line2 if lconf2 >= 0.60 else use_line

            ch2, prev_row2 = record_resource(cur, pdf_url, competitor, use_line2, "pdf", dl_headers2, dl_hash2, sub_text)
            if ch2 in ("added", "updated"):
                pdf_resp2 = safe_request("GET", pdf_url)
                pdf_bytes2 = pdf_resp2.content if pdf_resp2 else None
                if pdf_bytes2 is None:
                    pdf_resp2 = safe_request("GET", pdf_url)
                    pdf_bytes2 = pdf_resp2.content if pdf_resp2 else None

                disp_name2 = beautify_filename(pdf_url or "document.pdf")
                sha_now2 = sha256_bytes(pdf_bytes2) if pdf_bytes2 else (dl_hash2 or "nohash")
                archived_path2 = None
                archived_url2  = None
                if pdf_bytes2:
                    archived_path2 = archive_pdf(competitor, use_line2, pdf_url, pdf_bytes2, disp_name2, sha_now2)
                    if GITHUB_REPOSITORY:
                        archived_url2 = build_github_raw_url(GITHUB_REPOSITORY, GITHUB_REF_NAME, archived_path2)

                print(f"[ARCHIVE:SUB] {competitor} | {use_line2} | {disp_name2} -> {archived_path2 or 'NO BYTES'}")
                results.append({
                    "competitor": competitor,
                    "line": use_line2,
                    "url": pdf_url,
                    "what": doc_kind2,
                    "change": ch2,
                    "old_url": prev_row2["url"] if (prev_row2 and ch2 == "updated") else None,
                    "archived_url": archived_url2,
                    "archived_path": archived_path2
                })

    return results

def crawl_all(cur):
    events = []
    print("[SEEDS] Starting crawl across competitors/lines")
    for competitor, lines in (URLS_CONF or {}).items():
        for line in LINES_ORDER:
            urls = lines.get(line, []) or []
            for url in urls:
                print(f"[SEED] {competitor} | {line} | {url}")
                evs = crawl_seed(cur, competitor, line, url)
                events.extend(evs)  # politeness delay
                time.sleep(0.3)
    print(f"[SEEDS] Crawl finished with {len(events)} events")
    return events

# -------------------
# Pivot for email table
# -------------------
def pivot_for_table(all_events):
    competitors = COMPETITOR_COLS[:]  # fixed order
    extras = [e["competitor"] for e in all_events if e["competitor"] not in competitors]
    competitors += [c for c in sorted(set(extras))]

    table = {line: {c: [] for c in competitors} for line in LINES_ORDER}
    na_map = build_na_map()

    for e in all_events:
        c, line, what, change = e["competitor"], e["line"], e["what"], e["change"]
        if what in ("Spec Sheet", "Brochure", "Data Sheet"):
            if change == "updated":
                old_href = e.get("archived_url") or e.get("old_url") or e["url"]
                old_label = "Archived prior version" if e.get("archived_url") else "Old version"
                label = (
                    f'{what} updated: '
                    f'{a(e["url"], beautify_filename(e["url"]))} '
                    f'(old: {a(old_href, old_label)} → new: {a(e["url"], beautify_filename(e["url"]))})'
                )
            else:
                label = f'{what} {change}: {a(e["url"], beautify_filename(e["url"]))}'
            table[line][c].append(label)

        elif what == "Product page":
            short = display_url_label(e["url"], max_len=60)
            label = f'Product page {change}: {a(e["url"], short)}'
            table[line][c].append(label)
    
        elif what == "Product tile":
            # Prefer tile title; fallback to a compact label if missing
            label_txt = (e.get("title") or display_url_label(e["url"], max_len=60))
            label = f'Product tile {change}: {a(e["url"], label_txt)}'
            table[line][c].append(label)

    return competitors, table, na_map

# -------------------
# Icons for product lines
# -------------------
def line_icon_name(line: str):
    m = {
        "Door Type": ("doortype.png", os.path.join("assets", "doortype.png")),
        "Undercounter": ("undercounter.png", os.path.join("assets", "undercounter.png")),
        "Prep Washer": ("prepwasher.png", os.path.join("assets", "prepwasher.png")),
        "Rack Conveyor": ("rackconveyor.png", os.path.join("assets", "rackconveyor.png")),
        "Flight Type": ("flighttype.png", os.path.join("assets", "flighttype.png")),
    }
    return m.get(line, ("", ""))

# -------------------
# Email builder (HTML)
# -------------------
def compose_email(all_events):
    """
    Build the subject + HTML body:
    - Fixed column width per column (table-layout: fixed)
    - Shade first column same as header, show icon above product-line text
    - Shade cells with updates using EMAIL_UPDATE_BG
    - Show 'N/A' if urls.yaml has [] for that competitor+line
    """
    if all_events:
        comps = sorted({e["competitor"] for e in all_events})
        subject = "Daily WW Competitor monitor – " + ", ".join(comps)
    else:
        subject = "Daily WW Competitor monitor – No update"

    competitors, table, na_map = pivot_for_table(all_events)
    wrap_css = "white-space: normal; word-break: break-word; overflow-wrap: anywhere;"
    li_style = f"{wrap_css} margin:0 0 4px 0;"
    ul_style = f"margin:0 0 0 17px; padding-left:0; list-style-position: outside; {wrap_css}"

    html = []
    html.append(
        f"<div style='font-family:{EMAIL_FONT_FAMILY}; font-size:{EMAIL_FONT_SIZE}; background:{EMAIL_BODY_BG}'>"
    )
    html.append(f"<p><strong>Date:</strong> {datetime.now().strftime('%Y-%m-%d')}</p>")
    html.append(
        f"<table border='1' cellpadding='{EMAIL_CELL_PAD}' cellspacing='0' "
        f"style='border-collapse:collapse; width:100%; border-color:{EMAIL_BORDER_CLR}; table-layout:fixed;'>"
    )

    # Header row
    html.append("<thead><tr>")
    for col in (["Product Line"] + competitors):
        html.append(
            f"<th style='text-align:left; background:{EMAIL_HEADER_BG}; color:{EMAIL_HEADER_FG}; "
            f"width:{EMAIL_COL_WIDTH};'>{col}</th>"
        )
    html.append("</tr></thead><tbody>")

    # Rows
    for line in LINES_ORDER:
        html.append("<tr>")

        # First column with icon
        cid, _ = line_icon_name(line)
        icon_html = (
            f'<img src="cid:{cid}" alt="{line}" width="48" height="48" style="display:block; margin:0 0 4px 0;">'
            if cid else ""
        )
        html.append(
            f"<td style='font-weight:600; width:{EMAIL_COL_WIDTH}; {wrap_css} "
            f"background:{EMAIL_HEADER_BG}; color:{EMAIL_HEADER_FG}; padding:6px 8px; text-align:left;'>"
            f"{icon_html}{line}</td>"
        )

        # Competitor cells
        for c in competitors:
            items = table[line].get(c, [])
            cell_style = f"width:{EMAIL_COL_WIDTH}; {wrap_css}"
            if items:
                cell_style += f" background:{EMAIL_UPDATE_BG};"

            if not items:
                if na_map.get(line, {}).get(c, False):
                    cell_html = "<em>N/A</em>"
                else:
                    cell_html = "<em>No Update</em>"
            else:
                bullets = "".join(f"<li style='{li_style}'>{it}</li>" for it in items)
                cell_html = f"<ul style='{ul_style}'>{bullets}</ul>"

            html.append(f"<td style='{cell_style}'>{cell_html}</td>")

        html.append("</tr>")

    html.append("</tbody></table></div>")
    return subject, "\n".join(html)

# -------------------
# Senders (Graph / SMTP)
# -------------------
SEND_MODE = os.getenv("SEND_MODE", "GRAPH")  # GRAPH or SMTP
MAIL_TO   = os.getenv("MAIL_TO", "jaehan.kim@itwfeg.com")
MAIL_FROM = os.getenv("MAIL_FROM", "ww-monitor@itwfeg.com")

# Graph (app creds)
GRAPH_TENANT        = os.getenv("GRAPH_TENANT_ID", "")
GRAPH_CLIENT_ID     = os.getenv("GRAPH_CLIENT_ID", "")
GRAPH_CLIENT_SECRET = os.getenv("GRAPH_CLIENT_SECRET", "")

# SMTP (optional fallback)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

def _build_inline_attachments_for_lines() -> list[dict]:
    """
    Build Microsoft Graph inline fileAttachment objects for any icons that exist in ./assets.
    """
    attachments = []
    for line in LINES_ORDER:
        cid, path = line_icon_name(line)
        if not cid or not path or not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            data = f.read()
        attachments.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": cid,            # attachment filename
            "contentId": cid,       # must match cid in cid:...
            "isInline": True,       # inline display
            "contentBytes": base64.b64encode(data).decode("utf-8"),
            "contentType": "image/png"
        })
    return attachments

def _build_file_attachments(paths: list[str]) -> list[dict]:
    """
    Standard file attachments (non-inline) for Graph sendMail.
    """
    out = []
    for p in (paths or []):
        if not p or not os.path.exists(p):
            continue
        with open(p, "rb") as f:
            b = f.read()
        out.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": os.path.basename(p),
            "contentBytes": base64.b64encode(b).decode("utf-8"),
            "contentType": "text/csv" if p.lower().endswith(".csv") else "application/octet-stream",
            "isInline": False
        })
    return out

def send_via_graph(subject, html_body, file_paths: list[str] | None = None):
    token_url = f"https://login.microsoftonline.com/{GRAPH_TENANT}/oauth2/v2.0/token"
    data = {
        "client_id": GRAPH_CLIENT_ID,
        "client_secret": GRAPH_CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials"
    }
    dbg("[GRAPH] Requesting token…")
    r = requests.post(token_url, data=data, timeout=25)
    r.raise_for_status()
    access_token = r.json()["access_token"]

    inline_attachments = _build_inline_attachments_for_lines()
    file_attachments   = _build_file_attachments(file_paths or [])
    all_attachments    = inline_attachments + file_attachments

    send_url = f"https://graph.microsoft.com/v1.0/users/{MAIL_FROM}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": { "contentType": "HTML", "content": f"<html><body>{html_body}</body></html>" },
            "toRecipients": [{"emailAddress": {"address": addr.strip()}}
                             for addr in MAIL_TO.split(",") if addr.strip()]
        },
        "saveToSentItems": "true"
    }
    if all_attachments:
        payload["message"]["attachments"] = all_attachments

    h = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    dbg("[GRAPH] Sending email…")
    rr = requests.post(send_url, headers=h, json=payload, timeout=25)
    rr.raise_for_status()
    dbg("[GRAPH] Email sent.")

def send_via_smtp(subject, html_body):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = MAIL_FROM
    msg["To"]      = MAIL_TO
    msg.attach(MIMEText(html_body, "html"))

    dbg("[SMTP] Connecting…")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(MAIL_FROM, [a.strip() for a in MAIL_TO.split(",") if a.strip()], msg.as_string())
    dbg("[SMTP] Email sent.")

# -------------------
# Git commit & push for archives
# -------------------
def git_commit_and_push(paths: list[str], message: str = "chore: archive updated PDFs"):
    """
    Commit and push the given files to the current branch (uses GITHUB_TOKEN in Actions).
    No-op if 'paths' is empty or running outside GitHub Actions without creds.
    """
    paths = [p for p in (paths or []) if p]
    if not paths:
        print("[ARCHIVE] No files to commit.")
        return

    if not os.getenv("GITHUB_ACTIONS"):
        # local dev: skip auto-commit to avoid unintended pushes
        print("[ARCHIVE] Skipping git push (not in GitHub Actions). Files saved locally:")
        for p in paths:
            print(" -", p)
        return

    os.system('git config user.email "actions@github.com"')
    os.system('git config user.name "github-actions[bot]"')

    for p in paths:
        os.system(f'git add "{p}"')

    commit_rc = os.system(f'git commit -m "{message}"')
    if commit_rc != 0:
        print("[ARCHIVE] Nothing to commit (or commit failed).")

    push_rc = os.system('git push')
    if push_rc != 0:
        print("[ARCHIVE] Push failed (check workflow permissions).")
    else:
        print("[ARCHIVE] Pushed archive commit.")

# -------------------
# Preview utilities (optional)
# -------------------
def write_preview_file(subject: str, body: str, fname: str = "preview.html"):
    """Writes a standalone HTML file for easy preview in SAMPLE mode."""
    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{subject}</title>
</head>
<body>
{body}
</body>
</html>"""
    with open(fname, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[TEST MODE] Wrote {fname} with rendered email HTML.")

def sample_events_for_preview():
    return [
        {"competitor":"Champion","line":"Rack Conveyor","url":"https://www.championindustries.com/content/spec-sheets/Rack-Conveyors/44-PRO-VHR_Electric_Rev.09-2025.pdf","what":"Spec Sheet","change":"updated","old_url":"https://www.championindustries.com/content/spec-sheets/Rack-Conveyors/44-PRO-VHR_Electric_Rev.08-2025.pdf","archived_url":None,"archived_path":None},
        {"competitor":"Jackson","line":"Rack Conveyor","url":"https://www.jacksonwws.com/wp-content/uploads/2026/02/RackStar_66_ER_brochure.pdf","what":"Brochure","change":"added","old_url":None,"archived_url":None,"archived_path":None},
        {"competitor":"CMA","line":"Door Type","url":"https://cmadishmachines.com/product/model-180-straight/","what":"Product page","change":"updated","old_url":None,"archived_url":None,"archived_path":None},
        {"competitor":"Meiko","line":"Undercounter","url":"https://www.meiko.com/en-us/products/commercial-dishwashers/undercounter-dishwashers/fv-402-g","what":"Product page","change":"added","old_url":None,"archived_url":None,"archived_path":None},
        {"competitor":"Douglas","line":"Prep Washer","url":"https://www.dougmac.com/wp-content/uploads/2024/08/Product-Sheet-Bucket-Pan-Washer.pdf","what":"Brochure","change":"added","old_url":None,"archived_url":None,"archived_path":None},
        {"competitor":"LVO","line":"Prep Washer","url":"https://www.lvomfg.com/site/product/fl36/","what":"Product page","change":"updated","old_url":None,"archived_url":None,"archived_path":None},
        {"competitor":"ADS","line":"Door Type","url":"https://www.americandish.com/product/upright-dish-machine-af-afc-es/","what":"Product page","change":"added","old_url":None,"archived_url":None,"archived_path":None},
        {"competitor":"Moyer Diebel","line":"Undercounter","url":"https://moyerdiebel.com/content/specs/383HT_Spec_Sheet.pdf","what":"Spec Sheet","change":"added","old_url":None,"archived_url":None,"archived_path":None},
        {"competitor":"Jackson","line":"Flight Type","url":"https://www.jacksonwws.com/products/flightstar/","what":"Product page","change":"updated","old_url":None,"archived_url":None,"archived_path":None},
    ]

# -------------------
# Post-crawl: build manifest (rules -> ML), pivot, return attachments
# -------------------
def build_reports_and_get_attachments() -> list[str]:
    """
    Runs rules classifier, ML fallback, and pivot builder.
    Returns list of file paths to attach to email.
    """
    if not _CLASSIFIER_AVAILABLE:
        print("[REPORT] Classifier modules not available; skipping attachments.")
        return []

    # 1) Rules pass -> manifest.rules.csv
    run_rules_only(write_symlinks=True)
    rules_csv = os.path.join(str(CLASS_OUT_DIR), "manifest.rules.csv")

    # 2) ML fallback -> manifest.csv
    import pandas as pd
    df_rules = pd.read_csv(rules_csv)
    apply_fallback(df_rules)  # writes output/manifest.csv
    manifest_csv = os.path.join(str(CLASS_OUT_DIR), "manifest.csv")

    # 3) Pivot -> report_by_product_line.csv
    build_pivot(manifest_csv, os.path.join(str(CLASS_OUT_DIR), "report_by_product_line.csv"))
    pivot_csv = os.path.join(str(CLASS_OUT_DIR), "report_by_product_line.csv")

    return [manifest_csv, pivot_csv]

# -------------------
# Main
# -------------------
def main():
    con = init_db()
    cur = con.cursor()

    # Ensure archive structure exists
    ensure_archive_tree()

    use_samples   = os.getenv("SAMPLE_EVENTS") == "1"
    force_preview = os.getenv("WRITE_PREVIEW") == "1"

    print(f"[INFO] BOOTSTRAP_ARCHIVE={os.getenv('BOOTSTRAP_ARCHIVE')} "
          f"SAMPLE_EVENTS={os.getenv('SAMPLE_EVENTS')} WRITE_PREVIEW={os.getenv('WRITE_PREVIEW')} "
          f"ARCHIVE_ALL_PDFS={os.getenv('ARCHIVE_ALL_PDFS')}")

    # --- BOOTSTRAP: crawl & archive, push, and exit (no email) ---
    if BOOTSTRAP and not use_samples:
        print("[BOOTSTRAP] Starting full archive …")
        all_events = crawl_all(cur)
        con.commit()
        archived_files = [e["archived_path"] for e in all_events if e.get("archived_path")]
        num_archived = len([p for p in archived_files if p])
        print(f"[BOOTSTRAP] Discovered {len(all_events)} events; archived files: {num_archived}")
        if num_archived:
            git_commit_and_push([p for p in archived_files if p], "bootstrap: initial PDF archive")
            print(f"[BOOTSTRAP] Archived and pushed {num_archived} PDFs.")
        else:
            print("[BOOTSTRAP] No PDFs discovered to archive (check seeds/logs).")
        print("[BOOTSTRAP] Done. Exiting without sending email.")
        return

    # --- Daily / Sample runs ---
    if use_samples:
        all_events = sample_events_for_preview()
    else:
        all_events = crawl_all(cur)

    # Persist crawl events
    for e in all_events:
        cur.execute("""
          INSERT INTO events(ts, competitor, line, url, what, change, archived_path, archived_url, title)
          VALUES(?,?,?,?,?,?,?,?,?)
        """, (
          datetime.now(timezone.utc).isoformat(),
          e["competitor"], e["line"], e["url"], e["what"], e["change"],
          e.get("archived_path"), e.get("archived_url"),
          e.get("title")             # may be None for non‑tile events
        ))
    con.commit()

    subject, body = compose_email(all_events)

    # Commit/push any archived PDFs we just saved
    archived_files = [e["archived_path"] for e in all_events if e.get("archived_path")]
    if archived_files:
        git_commit_and_push([p for p in archived_files if p], "chore: archive PDFs for today")

    # Build preview if enabled
    if use_samples or force_preview:
        write_preview_file(subject, body, "preview.html")

    # Build attachments (manifest.csv + pivot) if classifier modules present
    attachments = []
    try:
        attachments = build_reports_and_get_attachments()
    except Exception as ex:
        print(f"[REPORT] Failed to build attachments: {ex}")

    # Send
    if SEND_MODE.upper() == "GRAPH":
        # With no credentials or in SAMPLE mode, just print (no send)
        if not (GRAPH_TENANT and GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET) or use_samples:
            print("Graph credentials not set or SAMPLE mode enabled. Skipping send.")
            print("=== SUBJECT ==="); print(subject)
            print("=== HTML BODY ==="); print(body)
            if attachments:
                print("Attachments:", attachments)
            return
        send_via_graph(subject, body, file_paths=attachments)
    else:
        # SMTP fallback
        if use_samples:
            print("SAMPLE mode with SMTP selected—printing only.")
            print("=== SUBJECT ==="); print(subject)
            print("=== HTML BODY ==="); print(body)
            return
        send_via_smtp(subject, body)

# -------------------
# Entry point
# -------------------
if __name__ == "__main__":
    main()
