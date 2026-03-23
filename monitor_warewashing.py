
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WW Competitor Monitor
- Crawl seeds from config/urls.yaml
- Discover/Archive PDFs and product pages
- Deterministic product-line inference (brand-agnostic) + per-site YAML rules
- Tile-diff (new product tiles)
- Post-crawl reports and Graph email with attachments
"""
import os
import re
import sys
import sqlite3
import hashlib
import json
import time
import base64
import getpass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, unquote
import requests
from bs4 import BeautifulSoup


def extract_tile_links(html: str, base_url: str, competitor: str):
    """
    Return list[(href, title)] of likely 'product tile' anchors.
    Uses per-site CSS selectors if provided; otherwise falls back
    to common card/tile patterns.
    """
    out = []
    soup = BeautifulSoup(html or "", "lxml")
    seen = set()

    # 1) Site-specific selectors
    sels = _get_tile_selectors(competitor)
    for sel in (sels or []):
        for a in soup.select(sel):
            if a.name != "a":
                a = a.find("a", href=True)
            if not a or not a.get("href"):
                continue
            href = urljoin(base_url, a["href"])
            title = (a.get_text(" ", strip=True) or "").strip()
            if href not in seen:
                seen.add(href)
                out.append((href, title))

    # 2) Generic fallback: cards/tiles with a single prominent anchor
    if not out:
        for card in soup.select(".card, .tile, .product, .product-card, .grid-item"):
            a = card.find("a", href=True)
            if not a:
                continue
            href = urljoin(base_url, a["href"])
            # Prefer heading-like text inside the card
            title = ""
            for htag in ("h1","h2","h3","h4",".card-title",".product-title"):
                h = card.select_one(htag)
                if h and h.get_text(strip=True):
                    title = h.get_text(" ", strip=True)
                    break
            if not title:
                title = a.get_text(" ", strip=True)
            title = (title or "").strip()
            if href not in seen:
                seen.add(href)
                out.append((href, title))
    return out


# -------------------
# Optional: post-crawl classifier & reporting
# -------------------
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
URLS_CONF = load_yaml(os.path.join(CONFIG_DIR, "urls.yaml")) if os.path.exists(os.path.join(CONFIG_DIR, "urls.yaml")) else {}
STYLE_CONF = load_yaml(os.path.join(CONFIG_DIR, "styling.yaml")) if os.path.exists(os.path.join(CONFIG_DIR, "styling.yaml")) else {}
# Per-site rules (optional)
RULES_PATH = os.path.join(CONFIG_DIR, "site_rules.yaml")
SITE_RULES = load_yaml(RULES_PATH) if os.path.exists(RULES_PATH) else {}

# Safe fallbacks (add "Other" so pivot never fails)
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
if "Other" not in LINES_ORDER:
    LINES_ORDER = LINES_ORDER + ["Other"]  # prevent KeyError on uncategorized

DOOR, UNDER, PREP, RACK, FLIGHT = (
    "Door Type", "Undercounter", "Prep Washer", "Rack Conveyor", "Flight Type"
)

# -------------------
# Styling defaults
# -------------------
EMAIL_STYLE = STYLE_CONF.get("email", {}) if STYLE_CONF else {}
EMAIL_FONT_FAMILY = EMAIL_STYLE.get("font_family", "Segoe UI, Arial, sans-serif")
EMAIL_FONT_SIZE = EMAIL_STYLE.get("font_size", "14px")
EMAIL_HEADER_BG = EMAIL_STYLE.get("header_bg", "#f3f3f3")
EMAIL_HEADER_FG = EMAIL_STYLE.get("header_fg", "#000000")
EMAIL_BODY_BG = EMAIL_STYLE.get("body_bg", "#ffffff")
EMAIL_BORDER_CLR = EMAIL_STYLE.get("border_color", "#dddddd")
EMAIL_CELL_PAD = EMAIL_STYLE.get("cell_padding", "6")
EMAIL_COL_WIDTH = EMAIL_STYLE.get("column_width", "180px")
EMAIL_UPDATE_BG = EMAIL_STYLE.get("update_bg", "#FFFFE0")

# -------------------
# Archiving configuration
# -------------------
ARCHIVE_DIR = os.getenv("ARCHIVE_DIR", "archive")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main")
BOOTSTRAP = os.getenv("BOOTSTRAP_ARCHIVE") == "1"

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
        label = re.sub(r"[_-]+", " ", label).strip()
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
    name = re.sub(r"[_-]+", " ", name)
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
    for m in re.finditer(r'onclick\s*=\s*"(?:location\.href|window\.location)\s*=\s*([^\']+)[\'"]', html, re.I):
        out.add(urljoin(base, m.group(1)))
    for m in re.finditer(r"onclick\s*=\s*'(?:location\.href|window\.location)\s*=([^\"]+)[\"']", html, re.I):
        out.add(urljoin(base, m.group(1)))
    # data-url="/path/to/product/"
    for m in re.finditer(r'data-url\s*=\s*"([^"]+)"', html, re.I):
        out = out | {urljoin(base, m.group(1))}
    for m in re.finditer(r"data-url\s*=\s*'([^']+)'", html, re.I):
        out = out | {urljoin(base, m.group(1))}
    # JS helpers like goToProduct('/path/...') or openProduct("...")
    for m in re.finditer(r'(?:goToProduct|openProduct)\s*\(\s*([^\']+)[\'"]\s*\)', html, re.I):
        out.add(urljoin(base, m.group(1)))
    return out

# -------------------
# DCatalog: surface the original PDF download target from viewer pages
# -------------------

def extract_dcatalog_pdf_link(html: str, base_url: str) -> set[str]:
    out = set()
    if not html:
        return out
    soup = BeautifulSoup(html, "lxml")

    # 1) Visible anchors (explicit .pdf or text indicates PDF download)
    for a in soup.find_all("a", href=True):
        text = (a.get_text(" ", strip=True) or "").lower()
        href = a["href"]
        if ".pdf" in href.lower() or ("download" in text and "pdf" in text):
            out.add(urljoin(base_url, href))

    # 2) Script blocks (look for absolute *.pdf)
    pdf_rx = re.compile(r"https?://[^\s\"'>]+\.pdf(?:\?[^\s\"'>]*)?", re.I)
    for sc in soup.find_all("script"):
        content = sc.string or sc.get_text() or ""
        for p in pdf_rx.findall(content):
            out.add(urljoin(base_url, p))
    return out

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
    # Existing tables
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
    # Tiles table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS catalog_tiles(
            competitor TEXT,
            line TEXT,
            page_url TEXT,
            tile_title TEXT,
            tile_href TEXT,
            first_seen TEXT,
            last_seen TEXT,
            PRIMARY KEY (competitor, line, page_url, tile_href)
        )
    """)
    # Add columns once (idempotent)
    for col in ("title", "new_archived_url", "old_archived_url"):
        try:
            cur.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
        except Exception:
            pass
    con.commit()
    return con

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
    etag = headers.get("ETag") if headers else None
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
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

session = requests.Session()
# Strong, browser-like default headers (helps CF/WP)
session.headers.update({
    "User-Agent": os.getenv("UA_OVERRIDE", DEFAULT_UA),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # IMPORTANT: do NOT advertise 'br' so we avoid Brotli body w/out decoder
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
})

def prime_host_session(url: str):
    """Warm up cookies/challenge for the host (helps CF/WP/CDN)."""
    try:
        u = urlparse(url)
        root = f"{u.scheme}://{u.netloc}/"
        _ = session.get(root, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except Exception:
        pass

# --- OPTIONAL Cloudflare-aware fallback (CMA & Champion) ---
try:
    import cloudscraper
    _SCRAPER_AVAILABLE = True
except Exception:
    _SCRAPER_AVAILABLE = False

_SCRAPER_HOSTS = {
    "cmadishmachines.com", "www.cmadishmachines.com",
    "championindustries.com", "www.championindustries.com",
}

def _is_scraper_host(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return any(host == h or host.endswith("." + h) for h in _SCRAPER_HOSTS)
    except Exception:
        return False

def _should_use_scraper(url: str) -> bool:
    return _SCRAPER_AVAILABLE and os.getenv("USE_CLOUDSCRAPER", "0") == "1" and _is_scraper_host(url)

_scraper = None
def _get_scraper():
    """Create (lazy) scraper instance and mirror session headers."""
    global _scraper
    if _scraper is None:
        _scraper = cloudscraper.create_scraper()
        _scraper.headers.update(session.headers)
    return _scraper

def safe_request(method, url):
    """
    Primary HTTP entry. Uses requests.Session by default.
    If a 403 occurs on GET/HEAD for known scraper hosts and USE_CLOUDSCRAPER=1,
    retries once via cloudscraper. Carries Referer header when present.
    """
    try:
        r = session.request(method, url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if os.getenv("DEBUG_LOG", "0") == "1" and r is not None and r.status_code >= 400:
            print(f"[HTTP-{method}] {r.status_code} {r.reason} :: {url}")
        # If 403 and we want the scraper, try it before raising
        if r is not None and r.status_code in (403, 503) and _should_use_scraper(url) and method in ("GET", "HEAD"):
            try:
                sc = _get_scraper()
                headers = {}
                if "Referer" in session.headers:
                    headers["Referer"] = session.headers["Referer"]
                rr = sc.request(method, url, timeout=REQUEST_TIMEOUT, allow_redirects=True, headers=headers or None)
                if os.getenv("DEBUG_LOG", "0") == "1" and rr is not None and rr.status_code >= 400:
                    print(f"[HTTP-SCRAPER-{method}] {rr.status_code} {rr.reason} :: {url}")
                rr.raise_for_status()
                # optionally feed cookies back to session to help subsequent calls
                try:
                    session.cookies.update(rr.cookies)
                except Exception:
                    pass
                return rr
            except Exception as se:
                dbg(f"[SCRAPER-{method}] {url} -> {se}")
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
    if not r:
        return None, None
    # Debug: show content-encoding to verify no br appears anymore
    if os.getenv("DEBUG_LOG", "0") == "1":
        enc = r.headers.get("Content-Encoding")
        print(f"[HTTP] {url} :: Content-Encoding={enc}")
        body_preview = (r.text or "").strip()
        if len(body_preview) < 50:
            print(f"[HTML] Body looks empty/short for: {url}")
    return r.text, r.headers

def head(url):
    r = safe_request("HEAD", url)
    return r.headers if (r and r.status_code < 400) else None

def get_pdf_hash(url):
    r = safe_request("GET", url)
    if not r:
        return None, None
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

# ---- Deterministic precedence rules for product-line (brand-agnostic globals) ----
# Flight > Rack > Door > Undercounter > Prep Washer > Other
_RX_FLIGHT = [
    re.compile(r"\bFlight\s*Type\b", re.I),
    re.compile(r"\bFlight\s*Machine\b", re.I),
]
_RX_RACK = [
    re.compile(r"rack\s*conveyor", re.I),
]
_RX_DOOR = [
    re.compile(r"\bHood\s*Type\b", re.I),
    re.compile(r"\bDoor\s*Type\b", re.I),
    re.compile(r"\bTall\s*Hood\b", re.I),
]
# Improved Undercounter (glass washer variants)
_RX_UNDER = [
    re.compile(r"\bglass\s*washer(s)?\b", re.I),
    re.compile(r"\bglass\s*washing(?:\s*machine(s)?)?\b", re.I),
    re.compile(r"\bglasswasher(s)?\b", re.I),
    re.compile(r"\bundercounter\b", re.I),
]
# Expanded Prep Washer signals
_RX_PP = [
    re.compile(r"\bprep[\s\-]*washer(s)?\b", re.I),
    re.compile(r"\bpot[\s\-]*washer(s)?\b", re.I),
    re.compile(r"\bpot\s*(?:&|and)\s*pan(s)?\s*washer(s)?\b", re.I),
    re.compile(r"\bpan[\s\-]*washer(s)?\b", re.I),
    re.compile(r"\butensil(s)?\s*washer(s)?\b", re.I),
]
_RX_OTHER = [
    re.compile(r"waste\s*handling", re.I),
    re.compile(r"dehydrator", re.I),
    re.compile(r"\bCSS\b", re.I),
    re.compile(r"\bMRA\b", re.I),
]

def infer_line_global(text: str):
    # 1) Rack first
    for rx in _RX_RACK:
        if rx.search(text):
            return "Rack Conveyor", 0.95
    # 2) Flight
    for rx in _RX_FLIGHT:
        if rx.search(text):
            return "Flight Type", 0.90
    # 3) Door
    for rx in _RX_DOOR:
        if rx.search(text):
            return "Door Type", 0.90
    # 4) Under
    for rx in _RX_UNDER:
        if rx.search(text):
            return "Undercounter", 0.90
    # 5) Prep
    for rx in _RX_PP:
        if rx.search(text):
            return "Prep Washer", 0.90
    # 6) Other
    for rx in _RX_OTHER:
        if rx.search(text):
            return "Other", 0.95
    return "Other", 0.50

def infer_line_via_rules(url: str, anchor_text: str, page_text: str, competitor: str) -> tuple[str, float]:
    """
    Global deterministic precedence first (brand-agnostic),
    then per-site YAML rules (if present).
    """
    text = " ".join([url or "", anchor_text or "", page_text or ""])
    # 1) Global deterministic
    line, conf = infer_line_global(text)
    if conf >= 0.90:
        return line, conf
    # 2) Per-site rules
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
ABS_PDF_RX = re.compile(r'https?://[^"\'\<>]+?\.pdf(?:\?[^"\'\<>]*)?', re.I)
REL_PDF_RX = re.compile(r'(?:(?:\./|\../|/)[^"\'\<>\s]+?\.pdf(?:\?[^"\'\<>\s]*)?)', re.I)

def crawl_seed(cur, competitor, line, url):
    # PRIME host (important for CF/WP)
    prime_host_session(url)
    html, headers = get_html(url)
    results = []
    if not html:
        print(f"[CRAWL] NO HTML for {competitor} | {line} | {url}")
        return results

    title, links = extract_links(html, url)
    embedded = extract_embedded_links(html, url)
    if embedded:
        links.extend((e, "") for e in sorted(embedded))

    # DCatalog viewer special-case at seed level:
    # if the seed itself is a dcatalog viewer URL, surface its PDF
    try:
        if urlparse(url).netloc.lower().endswith("dcatalog.com"):
            dlinks = extract_dcatalog_pdf_link(html, url)
            if dlinks:
                if os.getenv("DEBUG_LOG","0") == "1":
                    print(f"[DCATALOG] Found {len(dlinks)} PDF link(s) at seed: {url}")
                links.extend((dl, "Download PDF") for dl in sorted(dlinks))
    except Exception as _ex:
        dbg(f"[DCATALOG] seed parse failed {url} -> {_ex}")

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

    # Anchor text map
    link_text_map = {}
    for href, text in links:
        if href not in link_text_map:
            link_text_map[href] = text or ""

    # Discover PDFs on seed page
    pdf_candidates = set()
    for m in ABS_PDF_RX.finditer(html):
        pdf_candidates.add(urljoin(url, m.group(0)))
    for m in REL_PDF_RX.finditer(html):
        pdf_candidates.add(urljoin(url, m.group(0)))
    for href, _t in links:
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
        else:
            dbg(f"[PDF-FILTER] filtered out (host disallowed): {href}")
    if filtered_pdf_candidates:
        pdf_candidates = filtered_pdf_candidates

    # Accept-all PDFs (env or site rule)
    archive_all = os.getenv("ARCHIVE_ALL_PDFS", "0") == "1" or rules.get("pdf_accept_all", False)

    # ---- 1) PDFs on seed
    seen = set()
    for href in sorted(pdf_candidates):
        if href in seen:
            continue
        seen.add(href)
        text = link_text_map.get(href, "")
        if not archive_all and not (PDF_PATTERNS.search(href) or PDF_PATTERNS.search(text)):
            continue
        # HEAD → fallback GET for hash/headers with Referer=seed page
        session.headers["Referer"] = url
        try:
            h = head(href)
            dl_hash, dl_headers = (None, h)
            if h is None or not (h.get("ETag") or h.get("Last-Modified")):
                dl_hash, dl_headers = get_pdf_hash(href)
        finally:
            session.headers.pop("Referer", None)
        doc_kind = classify_pdf(f"{href} {text}") or ("Brochure" if archive_all else None)
        if not doc_kind:
            continue
        inferred_line, conf = infer_line_via_rules(href, text, "", competitor)
        use_line = inferred_line if conf >= 0.60 else line
        change, prev_row = record_resource(cur, href, competitor, use_line, "pdf", dl_headers, dl_hash, text)
        if change in ("added", "updated"):
            # Download & archive (with Referer=seed page)
            session.headers["Referer"] = url
            try:
                pdf_resp = safe_request("GET", href)
                pdf_bytes = pdf_resp.content if pdf_resp else None
                if pdf_bytes is None:
                    pdf_resp = safe_request("GET", href)
                    pdf_bytes = pdf_resp.content if pdf_resp else None
            finally:
                session.headers.pop("Referer", None)
            disp_name = beautify_filename(href or text or "document.pdf")
            sha_now = sha256_bytes(pdf_bytes) if pdf_bytes else (dl_hash or "nohash")
            archived_path = None
            archived_url = None
            if pdf_bytes:
                archived_path = archive_pdf(competitor, use_line, href, pdf_bytes, disp_name, sha_now)
                if GITHUB_REPOSITORY:
                    archived_url = build_github_raw_url(GITHUB_REPOSITORY, GITHUB_REF_NAME, archived_path)
            print(f"[ARCHIVE] {competitor} | {use_line} | {disp_name} -> {archived_path or 'NO BYTES'}")
            results.append({
                "competitor": competitor,
                "line": use_line,
                "url": href,  # vendor URL
                "what": doc_kind,
                "change": change,
                "old_url": prev_row["url"] if (prev_row and change == "updated") else None,
                "archived_url": archived_url,  # keep for compatibility
                "new_archived_url": archived_url,
                "archived_path": archived_path
            })

    # ---- 2) One-hop product pages
    for href, text in links:
        u = urlparse(href)
        same_host = (u.netloc == host)
        # Allow DCatalog viewer pages even if cross-host (Jackson specs)
        allow_cross = u.netloc.lower().endswith("dcatalog.com")
        if not (same_host or allow_cross):
            continue
        if not looks_like_product_page(href, competitor):
            continue

        # GET subpage with Referer=seed page
        session.headers["Referer"] = url
        try:
            ph, ph_headers = get_html(href)
        finally:
            session.headers.pop("Referer", None)
        if not ph:
            print(f"[CRAWL] NO HTML (subpage) for {href}")
            continue

        ptitle, sub_links = extract_links(ph, href)
        embedded_sub = extract_embedded_links(ph, href)
        if embedded_sub:
            sub_links.extend((e, "") for e in sorted(embedded_sub))

        # DCatalog viewer special-case at subpage level
        try:
            if u.netloc.lower().endswith("dcatalog.com"):
                dlinks2 = extract_dcatalog_pdf_link(ph, href)
                if dlinks2:
                    if os.getenv("DEBUG_LOG","0") == "1":
                        print(f"[DCATALOG] Found {len(dlinks2)} PDF link(s) at subpage: {href}")
                    sub_links.extend((dl, "Download PDF") for dl in sorted(dlinks2))
        except Exception as _ex2:
            dbg(f"[DCATALOG] subpage parse failed {href} -> {_ex2}")

        page_text = strip_main_text(ph)
        inferred_line, conf = infer_line_via_rules(href, text, page_text, competitor)
        use_line = inferred_line if conf >= 0.60 else line
        content_hash = sha256_bytes(page_text.encode("utf-8"))
        change, _ = record_resource(cur, href, competitor, use_line, "html", ph_headers, content_hash, ptitle)
        if change in ("added", "updated"):
            results.append({
                "competitor": competitor, "line": use_line, "url": href,
                "what": "Product page", "change": change,
                "old_url": None, "archived_path": None, "archived_url": None
            })

        # PDFs on subpage
        sub_pdf_candidates = set()
        for m in ABS_PDF_RX.finditer(ph):
            sub_pdf_candidates.add(urljoin(href, m.group(0)))
        for m in REL_PDF_RX.finditer(ph):
            sub_pdf_candidates.add(urljoin(href, m.group(0)))
        for sub_href, _ in sub_links:
            if is_pdf(sub_href):
                sub_pdf_candidates.add(sub_href)

        filtered_sub_pdf_candidates = set()
        for pdf_url in sub_pdf_candidates:
            if _host_allowed(rules["pdf_host_allow"], urlparse(pdf_url).netloc):
                filtered_sub_pdf_candidates.add(pdf_url)
            else:
                dbg(f"[PDF-FILTER] filtered out (host disallowed): {pdf_url}")
        if filtered_sub_pdf_candidates:
            sub_pdf_candidates = filtered_sub_pdf_candidates

        for pdf_url in sorted(sub_pdf_candidates):
            sub_text = ""
            if not archive_all and not (PDF_PATTERNS.search(pdf_url) or PDF_PATTERNS.search(sub_text)):
                continue
            # HEAD/GET with Referer=subpage
            session.headers["Referer"] = href
            try:
                h2 = head(pdf_url)
                dl_hash2, dl_headers2 = (None, h2)
                if h2 is None or not (h2.get("ETag") or h2.get("Last-Modified")):
                    dl_hash2, dl_headers2 = get_pdf_hash(pdf_url)
            finally:
                session.headers.pop("Referer", None)
            doc_kind2 = classify_pdf(f"{pdf_url} {sub_text}") or ("Brochure" if archive_all else None)
            if not doc_kind2:
                continue
            page_line2, lconf2 = infer_line_via_rules(pdf_url, sub_text, "", competitor)
            use_line2 = page_line2 if lconf2 >= 0.60 else use_line
            ch2, prev_row2 = record_resource(cur, pdf_url, competitor, use_line2, "pdf", dl_headers2, dl_hash2, sub_text)
            if ch2 in ("added", "updated"):
                session.headers["Referer"] = href
                try:
                    pdf_resp2 = safe_request("GET", pdf_url)
                    pdf_bytes2 = pdf_resp2.content if pdf_resp2 else None
                    if pdf_bytes2 is None:
                        pdf_resp2 = safe_request("GET", pdf_url)
                        pdf_bytes2 = pdf_resp2.content if pdf_resp2 else None
                finally:
                    session.headers.pop("Referer", None)
                disp_name2 = beautify_filename(pdf_url or "document.pdf")
                sha_now2 = sha256_bytes(pdf_bytes2) if pdf_bytes2 else (dl_hash2 or "nohash")
                archived_path2 = None
                archived_url2 = None
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
                    "new_archived_url": archived_url2,
                    "archived_path": archived_path2
                })

    # --- Tile extraction + diff on seed/category page ---
    try:
        tiles = extract_tile_links(html, url, competitor)
        if tiles:
            prev = tiles_get_previous(cur, competitor, line, url)
            seen_hrefs = set()
            for thref, ttitle in tiles:
                seen_hrefs.add(thref)
                tiles_upsert(cur, competitor, line, url, thref, ttitle)
                tline, tconf = infer_line_via_rules(thref, ttitle, "", competitor)
                use_line_for_tile = tline if tconf >= 0.70 else line
                if thref not in prev:
                    results.append({
                        "competitor": competitor,
                        "line": use_line_for_tile,
                        "url": thref,
                        "title": ttitle,
                        "what": "Product tile",
                        "change": "added",
                        "old_url": None,
                        "archived_url": None,
                        "new_archived_url": None,
                        "archived_path": None
                    })
    except Exception as ex:
        dbg(f"[TILES] Failed on {url}: {ex}")
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
                events.extend(evs)
                time.sleep(0.3) # politeness delay
    print(f"[SEEDS] Crawl finished with {len(events)} events")
    return events



def get_last_update_date(cur, competitor, line):
    """
    Returns ISO timestamp of last real update for the competitor + line.
    Only counts 'added' or 'updated' events from daily/weekly runs.
    Does NOT count bootstrap or first-seen URLs.
    Returns None if no past updates exist.
    """
    cur.execute("""
        SELECT ts
        FROM events
        WHERE competitor=? 
          AND line=?
          AND change IN ('added', 'updated')
        ORDER BY ts DESC
        LIMIT 1
    """, (competitor, line))
    row = cur.fetchone()
    return row[0] if row else None




# -------------------
# Pivot for email table
# -------------------
def pivot_for_table(all_events, cur):
    competitors = COMPETITOR_COLS[:]
    extras = [e["competitor"] for e in all_events if e["competitor"] not in competitors]
    competitors += [c for c in sorted(set(extras))]
    table = {line: {c: [] for c in competitors} for line in LINES_ORDER}
    na_map = build_na_map()
    for e in all_events:
        c, line, what, change = e["competitor"], e["line"], e["what"], e["change"]
        if line not in table:
            line = "Other"
        if what in ("Spec Sheet", "Brochure", "Data Sheet"):
            if change == "updated":
                new_href = e.get("new_archived_url") or e.get("archived_url") or e["url"]
                new_label = "New archived" if (e.get("new_archived_url") or e.get("archived_url")) else "New (website)"
                old_href = e.get("old_archived_url") or e.get("old_url") or e["url"]
                old_label = "Old archived" if e.get("old_archived_url") else ("Old (website)" if e.get("old_url") else "Old (same URL)")
                label = (
                    f'{what} updated: '
                    f'{a(new_href, beautify_filename(e["url"]))} '
                    f'(old: {a(old_href, old_label)} → new: {a(new_href, new_label)})'
                )
            else:
                href = e.get("new_archived_url") or e.get("archived_url") or e["url"]
                label = f'{what} {change}: {a(href, beautify_filename(e["url"]))}'
            table[line][c].append(label)
        elif what == "Product page":
            short = display_url_label(e["url"], max_len=60)
            label = f'Product page {change}: {a(e["url"], short)}'
            table[line][c].append(label)
        elif what == "Product tile":
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
        "Other": ("other.png", os.path.join("assets", "other.png")),
    }
    return m.get(line, ("", ""))

# -------------------
# Email builder (HTML)
# -------------------
def compose_email(all_events, cur):
    """
    Build the subject + HTML body
    """
    if all_events:
        comps = sorted({e["competitor"] for e in all_events})
        subject = "Daily WW Competitor monitor – " + ", ".join(comps)
    else:
        subject = "Daily WW Competitor monitor – No update"
    competitors, table, na_map = pivot_for_table(all_events, cur)
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
        # First column with icon (kept same rendering approach)
        cid, _ = line_icon_name(line)
        icon_html = f"<img src='cid:{cid}' alt='{line} icon' style='height:16px; vertical-align:middle; margin-right:6px;'/>" if cid else ""
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
                # N/A if competitor has no URLs for this line
                if na_map.get(line, {}).get(c, False):
                    cell_html = "<em>N/A</em>"
                else:
                    # Look up last meaningful update
                    last = get_last_update_date(cur, c, line)
            
                    if last is None:
                        # Never updated yet (ignore bootstrap / first-seen)
                        cell_html = "<em>N/A</em>"
                    else:
                        # Convert TS to YYYY-MM-DD
                        dt = last.split("T")[0]
                        cell_html = f"<em>Last updated: {dt}</em>"
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

# Graph (app-only auth for shared mailbox)
GRAPH_TENANT        = os.getenv("GRAPH_TENANT_ID", "")
GRAPH_CLIENT_ID     = os.getenv("GRAPH_CLIENT_ID", "")
GRAPH_CLIENT_SECRET = os.getenv("GRAPH_CLIENT_SECRET", "")

# SMTP (optional fallback)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

def _build_inline_attachments_for_lines() -> list[dict]:
    """Build inline icon attachments for Graph."""
    attachments = []
    for line in LINES_ORDER:
        cid, path = line_icon_name(line)
        if not cid or not path or not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            data = f.read()
        attachments.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": cid,
            "contentId": cid,
            "isInline": True,
            "contentBytes": base64.b64encode(data).decode("utf-8"),
            "contentType": "image/png"
        })
    return attachments

def _build_file_attachments(paths: list[str]) -> list[dict]:
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

def get_user_token():
    """Get access token using username/password authentication (Resource Owner Password Credential flow)."""
    # Prompt for credentials
    username = input("Enter username (email): ")
    password = getpass.getpass("Enter password: ")
    
    token_url = f"https://login.microsoftonline.com/{GRAPH_TENANT}/oauth2/v2.0/token"
    data = {
        "grant_type": "password",
        "client_id": GRAPH_CLIENT_ID,
        "scope": "https://graph.microsoft.com/.default",
        "username": username,
        "password": password
    }
    
    dbg("[GRAPH] Authenticating with username/password…")
    r = requests.post(token_url, data=data, timeout=25)
    
    if r.status_code != 200:
        error_info = r.json()
        print(f"\nAuthentication failed!")
        print(f"Error: {error_info.get('error', 'Unknown error')}")
        print(f"Description: {error_info.get('error_description', 'No details')}")
        raise Exception(f"Authentication failed: {error_info}")
    
    dbg("[GRAPH] Authentication successful.")
    return r.json()["access_token"]

def send_via_graph(subject, html_body, file_paths=None):
    access_token = get_user_token()

    inline_attachments = _build_inline_attachments_for_lines()
    file_attachments   = _build_file_attachments(file_paths or [])
    all_attachments    = inline_attachments + file_attachments

    send_url = f"https://graph.microsoft.com/v1.0/me/sendMail"
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
    paths = [p for p in (paths or []) if p]
    if not paths:
        print("[ARCHIVE] No files to commit.")
        return
    if not os.getenv("GITHUB_ACTIONS"):
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
        {"competitor":"Champion","line":"Rack Conveyor","url":"https://www.championindustries.com/content/spec-sheets/Rack-Conveyors/44-PRO-VHR_Electric_Rev.09-2025.pdf","what":"Spec Sheet","change":"updated","old_url":"https://www.championindustries.com/content/spec-sheets/Rack-Conveyors/44-PRO-VHR_Electric_Rev.08-2025.pdf","archived_url":None,"new_archived_url":None,"archived_path":None},
        {"competitor":"Jackson","line":"Rack Conveyor","url":"https://www.jacksonwws.com/wp-content/uploads/2026/02/RackStar_66_ER_brochure.pdf","what":"Brochure","change":"added","old_url":None,"archived_url":None,"new_archived_url":None,"archived_path":None},
        {"competitor":"CMA","line":"Door Type","url":"https://cmadishmachines.com/product/model-180-straight/","what":"Product page","change":"updated","old_url":None,"archived_url":None,"new_archived_url":None,"archived_path":None},
        {"competitor":"Meiko","line":"Undercounter","url":"https://www.meiko.com/en-us/products/commercial-dishwashers/undercounter-dishwashers/fv-402-g","what":"Product page","change":"added","old_url":None,"archived_url":None,"new_archived_url":None,"archived_path":None},
        {"competitor":"Douglas","line":"Prep Washer","url":"https://www.dougmac.com/wp-content/uploads/2024/08/Product-Sheet-Bucket-Pan-Washer.pdf","what":"Brochure","change":"added","old_url":None,"archived_url":None,"new_archived_url":None,"archived_path":None},
        {"competitor":"LVO","line":"Prep Washer","url":"https://www.lvomfg.com/site/product/fl36/","what":"Product page","change":"updated","old_url":None,"archived_url":None,"new_archived_url":None,"archived_path":None},
        {"competitor":"ADS","line":"Door Type","url":"https://www.americandish.com/product/upright-dish-machine-af-afc-es/","what":"Product page","change":"added","old_url":None,"archived_url":None,"new_archived_url":None,"archived_path":None},
        {"competitor":"Moyer Diebel","line":"Undercounter","url":"https://moyerdiebel.com/content/specs/383HT_Spec_Sheet.pdf","what":"Spec Sheet","change":"added","old_url":None,"archived_url":None,"new_archived_url":None,"archived_path":None},
        {"competitor":"Jackson","line":"Flight Type","url":"https://www.jacksonwws.com/products/flightstar/","what":"Product page","change":"updated","old_url":None,"archived_url":None,"new_archived_url":None,"archived_path":None},
    ]

# -------------------
# Post-crawl: build manifest (rules -> ML), pivot, return attachments
# -------------------
def build_reports_and_get_attachments() -> list[str]:
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
# Exports for audit (PDF inventory and today's PDF events)
# -------------------

def export_pdf_lists(cur, run_ts: str):
    os.makedirs("output", exist_ok=True)

    # A. All known PDFs (from resources) -> output/all_pdfs.csv
    cur.execute("""
        SELECT competitor, line, kind, url, last_modified, etag, hash, last_seen
        FROM resources
        WHERE kind='pdf'
        ORDER BY competitor, line, url
    """)
    rows = cur.fetchall()
    with open("output/all_pdfs.csv", "w", encoding="utf-8") as f:
        f.write("competitor,line,kind,url,last_modified,etag,hash,last_seen\n")
        for r in rows:
            f.write(",".join('' if (x is None) else str(x).replace(',', ' ') for x in r) + "\n")

    # B. PDFs that fired events in THIS run -> output/run_pdf_events.csv
    cur.execute("""
        SELECT ts,competitor,line,what AS kind,change,url,archived_path,archived_url,new_archived_url
        FROM events
        WHERE what IN ('Spec Sheet','Data Sheet','Brochure')
          AND ts = ?
        ORDER BY ts DESC, competitor, line
    """, (run_ts,))
    evs = cur.fetchall()
    with open("output/run_pdf_events.csv", "w", encoding="utf-8") as f:
        f.write("ts,competitor,line,kind,change,url,archived_path,archived_url,new_archived_url\n")
        for r in evs:
            f.write(",".join('' if (x is None) else str(x).replace(',', ' ') for x in r) + "\n")


# -------------------
# Main
# -------------------

def main():
    con = init_db()
    cur = con.cursor()

    # Ensure archive structure exists
    ensure_archive_tree()

    use_samples = os.getenv("SAMPLE_EVENTS") == "1"
    force_preview = os.getenv("WRITE_PREVIEW") == "1"
    weekly_mode = os.getenv("SEND_WEEKLY") == "1"

    print(
        f"[INFO] BOOTSTRAP_ARCHIVE={os.getenv('BOOTSTRAP_ARCHIVE')} "
        f"SAMPLE_EVENTS={os.getenv('SAMPLE_EVENTS')} "
        f"WRITE_PREVIEW={os.getenv('WRITE_PREVIEW')} "
        f"ARCHIVE_ALL_PDFS={os.getenv('ARCHIVE_ALL_PDFS')} "
        f"SEND_WEEKLY={weekly_mode}"
    )

    # -------------------------------------------------------
    # BOOTSTRAP MODE (crawl → archive → push → exit)
    # -------------------------------------------------------
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

    # -------------------------------------------------------
    # DAILY / SAMPLE RUN CRAWL
    # -------------------------------------------------------
    if use_samples:
        all_events = sample_events_for_preview()
    else:
        all_events = crawl_all(cur)

    # Run timestamp (consistent for whole batch)
    run_ts = datetime.now(timezone.utc).isoformat()

    # -------------------------------------------------------
    # Write event rows to DB (resources already updated earlier)
    # -------------------------------------------------------
    for e in all_events:
        cur.execute(
            """
            INSERT INTO events(ts, competitor, line, url, what, change,
                               archived_path, archived_url, title,
                               new_archived_url, old_archived_url)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_ts,
                e["competitor"],
                e["line"],
                e["url"],
                e["what"],
                e["change"],
                e.get("archived_path"),
                e.get("archived_url"),
                e.get("title"),
                e.get("new_archived_url"),
                e.get("old_archived_url"),
            )
        )
    con.commit()

    # -------------------------------------------------------
    # Daily Email-Sending Logic
    # (Option A: send only if there ARE updates)
    # -------------------------------------------------------
    if not weekly_mode:
        if not all_events:
            print("[DAILY] No updates → No email sent.")
            return  # <-- suppress email on no-update days

    # -------------------------------------------------------
    # Weekly Digest Mode (always sends)
    # -------------------------------------------------------
    if weekly_mode:
        print("[WEEKLY] Weekly digest mode enabled.")

        # Load last 7 days of events
        cur.execute("""
            SELECT ts, competitor, line, url, what, change,
                   archived_path, archived_url, new_archived_url, old_archived_url, title
            FROM events
            WHERE ts >= datetime('now', '-7 days')
            ORDER BY ts DESC
        """)
        rows = cur.fetchall()

        # Convert DB rows → event dicts
        weekly_events = []
        for r in rows:
            weekly_events.append({
                "ts": r[0],
                "competitor": r[1],
                "line": r[2],
                "url": r[3],
                "what": r[4],
                "change": r[5],
                "archived_path": r[6],
                "archived_url": r[7],
                "new_archived_url": r[8],
                "old_archived_url": r[9],
                "title": r[10],
            })

        subject, body = compose_email(weekly_events, cur)

        # Build attachments
        attachments = []
        try:
            attachments = build_reports_and_get_attachments()
        except Exception as ex:
            print(f"[REPORT] Failed to build attachments: {ex}")

        # Send weekly digest
        if SEND_MODE.upper() == "GRAPH":
            send_via_graph(subject, body, file_paths=attachments)
        else:
            send_via_smtp(subject, body)

        print("[WEEKLY] Digest email sent.")
        return

    # -------------------------------------------------------
    # DAILY EMAIL (updates only)
    # -------------------------------------------------------
    # Export audit CSVs
    export_pdf_lists(cur, run_ts)

    # Build email HTML
    subject, body = compose_email(all_events, cur)

    # Push new PDF archives
    archived_files = [e["archived_path"] for e in all_events if e.get("archived_path")]
    if archived_files:
        git_commit_and_push([p for p in archived_files if p], "chore: archive PDFs for today")

    # Preview if enabled
    if use_samples or force_preview:
        write_preview_file(subject, body, "preview.html")

    # Build attachments
    attachments = []
    try:
        attachments = build_reports_and_get_attachments()
    except Exception as ex:
        print(f"[REPORT] Failed to build attachments: {ex}")

    # Send via Graph or SMTP
    if SEND_MODE.upper() == "GRAPH":
        if not (GRAPH_TENANT and GRAPH_CLIENT_ID) or use_samples:
            print("Graph credentials not set or SAMPLE mode → printing only.")
            print("=== SUBJECT ==="); print(subject)
            print("=== HTML BODY ==="); print(body)
            if attachments:
                print("Attachments:", attachments)
            return

        send_via_graph(subject, body, file_paths=attachments)
    else:
        if use_samples:
            print("SMTP + SAMPLE mode → printing only.")
            print("=== SUBJECT ==="); print(subject)
            print("=== HTML BODY ==="); print(body)
            return

        send_via_smtp(subject, body)

# -------------------
# Entry point
# -------------------
if __name__ == "__main__":
    main()
