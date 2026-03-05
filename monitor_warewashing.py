
# monitor_warewashing.py (DEBUG VERSION)
# ---------------------------------------------------------------------------
# Debug version includes:
# - LOG_LINKS=1 to print extracted <a> links from HTML
# - ARCHIVE_ALL_PDFS=1 to archive EVERY .pdf found (ignores classification)
# - Extra debug printouts in crawl logic
# - Cross-host PDF discovery (CDNs/subdomains allowed for PDFs)
# - Raw HTML PDF extraction (absolute + relative) on seeds and subpages
# - Safer GET fallback for PDF downloads
# - Embedded product link extraction (onclick/data-url/JS helpers)
# - Optional UA override via UA_OVERRIDE
# ---------------------------------------------------------------------------

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
# Debug helper
# -------------------
def dbg(msg: str):
    """Lightweight debug print (enable with DEBUG_LOG=1)."""
    if os.getenv("DEBUG_LOG", "1") == "1":
        print(msg)


# --- add near other config loads (top of file) ---
RULES_PATH = os.path.join(CONFIG_DIR, "site_rules.yaml")
try:
    SITE_RULES = load_yaml(RULES_PATH)
except Exception:
    SITE_RULES = {}

def _compile_patterns(patts):
    if not patts:
        return []
    if isinstance(patts, (list, tuple)):
        return [re.compile(p) for p in patts]
    return [re.compile(patts)]

def _get_rules_for(competitor: str):
    r = SITE_RULES.get(competitor, {}) or {}
    return {
        "host_allow": _compile_patterns(r.get("host_allow")),
        "path_block": _compile_patterns(r.get("path_block")),
        "page_allow": _compile_patterns(r.get("page_allow")),
        "pdf_host_allow": _compile_patterns(r.get("pdf_host_allow")),
        "line_patterns": {
            k: _compile_patterns(v) for k, v in (r.get("line_patterns") or {}).items()
        },
        "pdf_path_line_hints": {k: re.compile(v) for k, v in (r.get("pdf_path_line_hints") or {}).items()}
    }

def _host_allowed(host_allow, netloc: str) -> bool:
    return (not host_allow) or any(p.search(netloc) for p in host_allow)

def _path_blocked(path_block, path: str) -> bool:
    return any(p.search(path) for p in path_block)

def _page_allowed(page_allow, path: str) -> bool:
    return any(p.search(path) for p in page_allow)

def infer_line_for_champion(url: str, anchor_text: str = "", page_text: str = "") -> tuple[str, float]:
    """Return (line, confidence) using Champion-specific rules from site_rules.yaml."""
    rules = _get_rules_for("Champion")
    text = " ".join([url or "", anchor_text or "", page_text or ""])
    # 1) PDF folder hints (highest confidence)
    for line, rx in (rules["pdf_path_line_hints"] or {}).items():
        if rx.search(url):
            return line, 0.95
    # 2) explicit line regex
    scores = {}
    for line, patt_list in (rules["line_patterns"] or {}).items():
        for p in patt_list:
            if p.search(text):
                scores[line] = scores.get(line, 0) + 1
    if scores:
        best = max(scores, key=scores.get)
        # confidence: normalized simple score
        return best, min(0.90, 0.65 + 0.1 * scores[best])
    return "Uncategorized", 0.0

# --- replace the old looks_like_product_page with a rules-aware version ---
def looks_like_product_page(url: str, competitor: str) -> bool:
    u = urlparse(url)
    path = u.path or "/"
    # Global quick rejects
    if any(x in url.lower() for x in ("/privacy", "/terms", "/sitemap", "/contact", "/search", "/careers")):
        return False
    # Per-competitor rules
    r = _get_rules_for(competitor)
    if r["path_block"] and _path_blocked(r["path_block"], path):
        return False
    if r["page_allow"]:
        # Only allow what we explicitly whitelisted for this competitor
        return _page_allowed(r["page_allow"], path)
    # Fallback to original heuristic for competitors without site_rules
    return any(x in url.lower() for x in ("/product", "/products/", "/our-products", "/rack", "/door", "/flight", "/dish", "/washer", "/categories/"))


# -------------------
# Config loading (YAML)
# -------------------
def load_yaml(path):
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


BASE_DIR = os.path.dirname(__file__)
CONFIG_DIR = os.path.join(BASE_DIR, "config")

COMP_CONF  = load_yaml(os.path.join(CONFIG_DIR, "competitors.yaml"))
URLS_CONF  = load_yaml(os.path.join(CONFIG_DIR, "urls.yaml"))
STYLE_CONF = load_yaml(os.path.join(CONFIG_DIR, "styling.yaml"))

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
LINES_ORDER     = COMP_CONF.get("lines", DEFAULT_LINES)

DOOR, UNDER, PREP, RACK, FLIGHT = (
    "Door Type", "Undercounter", "Prep Washer", "Rack Conveyor", "Flight Type"
)


# -------------------
# Styling defaults
# -------------------
EMAIL_STYLE       = STYLE_CONF.get("email", {})
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
ARCHIVE_DIR        = os.getenv("ARCHIVE_DIR", "archive")
GITHUB_REPOSITORY  = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_REF_NAME    = os.getenv("GITHUB_REF_NAME", "main")
BOOTSTRAP          = os.getenv("BOOTSTRAP_ARCHIVE") == "1"


# -------------------
# Build N/A matrix from urls.yaml
# -------------------
def build_na_map():
    na = {line: {c: False for c in COMPETITOR_COLS} for line in LINES_ORDER}
    for comp in COMPETITOR_COLS:
        comp_lines = URLS_CONF.get(comp, {})
        for line in LINES_ORDER:
            urls = comp_lines.get(line, None)
            if isinstance(urls, list) and len(urls) == 0:
                na[line][comp] = True
    return na


# -------------------
# HTML link helper
# -------------------
def a(href: str, label: str) -> str:
    href = href or "#"
    label = label or (href if href != "#" else "link")
    return f'<a href="{href}">{label}</a>'



# -------------------
# Display helpers
# -------------------
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


ACRONYM_KEEP = {"PRO","VHR","ER","HT","LT","HR","ADA","NSF","UL"}

def beautify_filename(url_or_name: str) -> str:
    name = unquote(url_or_name.split("?")[0].split("#")[0].split("/")[-1])
    name = re.sub(r"\.pdf$", "", name, flags=re.I)
    name = re.sub(r"[_\-]+", " ", name)
    name = re.sub(r"\b(spec(\.?|ification)?\s*sheet)\b", "Spec Sheet", name, flags=re.I)
    name = re.sub(r"\bdata\s*sheet|datasheet\b", "Data Sheet", name, flags=re.I)
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
    name = " ".join(words)
    return name


# -------------------
# PDF patterns (debug: broader)
# -------------------
PDF_PATTERNS = re.compile(
    r"(spec(?:ification)?[\s\-]?sheet|specsheet|data[\s\-]?sheet|datasheet|"
    r"cut[\s\-]?sheet|cutsheet|sell[\s\-]?sheet|sales[\s\-]?sheet|"
    r"product[\s\-]?sheet|product[\s\-]?data|technical[\s\-]?data|tech[\s\-]?data|"
    r"brochure|flyer)",
    re.I
)

def classify_pdf(text_or_url: str):
    s = text_or_url or ""
    if re.search(r"spec(?:ification)?[\s\-]?sheet|specsheet|cut[\s\-]?sheet|cutsheet", s, re.I):
        return "Spec Sheet"
    if re.search(r"data[\s\-]?sheet|datasheet|product[\s\-]?data|technical[\s\-]?data|tech[\s\-]?data", s, re.I):
        return "Data Sheet"
    if re.search(r"brochure|sell[\s\-]?sheet|sales[\s\-]?sheet|flyer|product[\s\-]?sheet", s, re.I):
        return "Brochure"
    return None


# -------------------
# DB helpers
# -------------------
DB_PATH = os.getenv("STATE_DB", "state.db")

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
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
    con.commit()
    return con

def get_existing_resource(cur, url):
    cur.execute("""
        SELECT url, competitor, line, kind, last_modified, etag, hash, title
        FROM resources WHERE url=?
    """, (url,))
    row = cur.fetchone()
    if row:
        keys = ["url","competitor","line","kind","last_modified","etag","hash","title"]
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
            SET last_modified=?, etag=?, hash=?, title=?, last_seen=?
            WHERE url=?
        """, (last_mod, etag, content_hash or prev_hash, title, now, url))
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
    h = hashlib.sha256(); h.update(b); return h.hexdigest()

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
    title = (soup.title.string.strip() if soup.title else "")
    for a_tag in soup.find_all("a", href=True):
        href = urljoin(base, a_tag["href"])
        text = (a_tag.get_text(" ", strip=True) or "")
        links.append((href, text))
    return title, links

def is_pdf(url):
    return url.lower().split("?")[0].endswith(".pdf")

def strip_main_text(html):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["nav","header","footer","script","style","noscript","svg"]):
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
    Save the given PDF bytes under archive/<competitor>/<line>/DATE__NAME__sha256_HASH.pdf
    Returns the local repo path of the archived PDF.
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
    """Build raw GitHub URL to the archived file."""
    local_path = local_path.replace("\\", "/")
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{local_path}"


# -------------------
# Crawl logic (DEBUG)
# -------------------
def looks_like_product_page(url):
    bad = ("/privacy", "/terms", "/sitemap", "/contact", "/search", "/news", "/blog", "/careers")
    if any(x in url.lower() for x in bad): return False
    return any(x in url.lower() for x in (
        "/product", "/products/", "/our-products", "/rack", "/door", "/flight", "/dish", "/washer", "/categories/"
    ))

def crawl_seed(cur, competitor, line, url):
    html, headers = get_html(url)
    results = []
    if not html:
        print(f"[CRAWL] NO HTML for {competitor} | {line} | {url}")
        return results

    title, links = extract_links(html, url)
    print(f"[CRAWL] {competitor} | {line} | {url} -> {len(links)} links")

    if os.getenv("LOG_LINKS", "0") == "1":
        for i, (lhref, ltext) in enumerate(links[:30]):
            print(f"   [LINK {i+1:02d}] {lhref}  |  {ltext[:120]}")

    # --- Add embedded product URLs that are not in <a href> ---
    embedded_links = set()

    # onclick="location.href='...'" or onclick="window.location='...'"
    for m in re.finditer(r'onclick\s*=\s*"(?:location\.href|window\.location)\s*=\s*[\'"]([^\'"]+)[\'"]', html, re.I):
        embedded_links.add(urljoin(url, m.group(1)))
    for m in re.finditer(r"onclick\s*=\s*'(?:location\.href|window\.location)\s*=\s*[\"']([^\"']+)[\"']", html, re.I):
        embedded_links.add(urljoin(url, m.group(1)))

    # data-url="/path/to/product/"
    for m in re.finditer(r'data-url\s*=\s*"([^"]+)"', html, re.I):
        embedded_links.add(urljoin(url, m.group(1)))
    for m in re.finditer(r"data-url\s*=\s*'([^']+)'", html, re.I):
        embedded_links.add(urljoin(url, m.group(1)))

    # JS helpers like goToProduct('/path/...') or openProduct("...")
    for m in re.finditer(r'(?:goToProduct|openProduct)\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)', html, re.I):
        embedded_links.add(urljoin(url, m.group(1)))

    if embedded_links:
        print(f"[CRAWL] {competitor} | {line} | {url} -> +{len(embedded_links)} embedded product links")
        for eurl in sorted(embedded_links):
            links.append((eurl, ""))  # treat same as normal anchors

    # Record the seed page itself (as a product/category page)
    change, _ = record_resource(
        cur, url, competitor, line, "html",
        headers, sha256_bytes(strip_main_text(html).encode("utf-8")), title
    )
    if change in ("added", "updated"):
        results.append({
            "competitor": competitor, "line": line, "url": url,
            "what": "Product page", "change": change,
            "old_url": None, "archived_path": None, "archived_url": None
        })

    # Build anchor text map to help classify PDFs when only filenames exist
    link_text_map = {}
    for href, text in links:
        if href not in link_text_map:
            link_text_map[href] = text or ""

# in crawl_seed(), right before we process PDFs:
host = urlparse(url).netloc
rules = _get_rules_for(competitor)

# 1) Filter PDF candidates by allowed host (Champion-tight, others unchanged)
pdf_candidates_filtered = set()
for href in pdf_candidates:
    h = urlparse(href).netloc
    if _host_allowed(rules["pdf_host_allow"], h):
        pdf_candidates_filtered.add(href)
pdf_candidates = pdf_candidates_filtered or pdf_candidates  # fallback if no rules

# ... when iterating PDFs (both on seed and subpages), infer correct product line:
line_for_archive, conf = infer_line_for_champion(href, link_text_map.get(href, ""), "")
use_line = line_for_archive if conf >= 0.6 else line  # keep seed line if low confidence

change, prev_row = record_resource(cur, href, competitor, use_line, "pdf", dl_headers, dl_hash, text)
# and pass `use_line` to archive_pdf(...)
archived_path = archive_pdf(competitor, use_line, href, pdf_bytes, disp_name, sha_now)

# 2) Subpage follow: use rules-aware looks_like_product_page
for href, text in links:
    if href in seen:
        continue
    if urlparse(href).netloc != host:
        continue
    if looks_like_product_page(href, competitor):
        ph, ph_headers = get_html(href)
        if not ph:
            # ...
            continue
        ptitle, sub_links = extract_links(ph, href)
        page_text = strip_main_text(ph)
        # infer line again for page classification
        page_line, lconf = infer_line_for_champion(href, text, page_text)
        use_line = page_line if lconf >= 0.6 else line
        content_hash = sha256_bytes(page_text.encode("utf-8"))
        change, _ = record_resource(cur, href, competitor, use_line, "html", ph_headers, content_hash, ptitle)
        # ...

    # Discover PDFs in seed HTML (absolute + relative) + anchor href PDFs
    pdf_candidates = set()
    # Absolute URLs: https://...pdf
    for m in re.finditer(r'https?://[^\s"\'<>]+\.pdf(?:\?[^\s"\'<>]*)?', html, re.I):
        pdf_candidates.add(urljoin(url, m.group(0)))
    # Relative URLs: /file.pdf or ./file.pdf or ../file.pdf
    for m in re.finditer(r'(?:(?:\./|\../|/)[^"\'<>\s]+\.pdf(?:\?[^\s"\'<>]*)?)', html, re.I):
        pdf_candidates.add(urljoin(url, m.group(0)))
    # Merge anchor href PDFs
    for href, _ in links:
        if is_pdf(href):
            pdf_candidates.add(href)

    print(f"[CRAWL] {competitor} | {line} | {url} -> {len(pdf_candidates)} pdf-candidates")
    if pdf_candidates:
        for i, cand in enumerate(sorted(list(pdf_candidates))[:20]):
            print(f"   [PDF CAND {i+1:02d}] {cand}")

    host = urlparse(url).netloc
    seen = set()

    archive_all = os.getenv("ARCHIVE_ALL_PDFS", "0") == "1"

    # 1) Handle PDFs first (cross-host allowed)
    for href in sorted(pdf_candidates):
        if href in seen:
            continue
        seen.add(href)

        text = link_text_map.get(href, "")

        # Accept if override or pattern-match
        if not archive_all:
            if not (PDF_PATTERNS.search(href) or PDF_PATTERNS.search(text)):
                continue

        # Try HEAD; fallback to GET for hash/headers
        h = head(href)
        dl_hash, dl_headers = (None, h)
        if h is None or not (h.get("ETag") or h.get("Last-Modified")):
            dl_hash, dl_headers = get_pdf_hash(href)

        # Classify (fallback to Brochure under override)
        doc_kind = classify_pdf(href + " " + text)
        if not doc_kind:
            doc_kind = "Brochure" if archive_all else None
        if not doc_kind:
            continue

        change, prev_row = record_resource(cur, href, competitor, line, "pdf", dl_headers, dl_hash, text)
        if change in ("added", "updated"):
            # Download and archive
            pdf_resp = safe_request("GET", href)
            pdf_bytes = pdf_resp.content if pdf_resp else None
            if pdf_bytes is None:
                # Retry once in case of transient failures
                pdf_resp = safe_request("GET", href)
                pdf_bytes = pdf_resp.content if pdf_resp else None

            disp_name = beautify_filename(href or text or "document.pdf")
            sha_now   = sha256_bytes(pdf_bytes) if pdf_bytes else (dl_hash or "nohash")

            archived_path = None
            archived_url  = None
            if pdf_bytes:
                archived_path = archive_pdf(competitor, line, href, pdf_bytes, disp_name, sha_now)
                if GITHUB_REPOSITORY:
                    archived_url = build_github_raw_url(GITHUB_REPOSITORY, GITHUB_REF_NAME, archived_path)

            print(f"[ARCHIVE] {competitor} | {line} | {disp_name} -> {archived_path or 'NO BYTES'}")

            results.append({
                "competitor": competitor,
                "line": line,
                "url": href,
                "what": doc_kind,
                "change": change,
                "old_url": prev_row["url"] if (prev_row and change == "updated") else None,
                "archived_url": archived_url,
                "archived_path": archived_path
            })

    # 2) Crawl product pages (same-host only) one hop
    for href, text in links:
        if href in seen:
            continue
        if urlparse(href).netloc != host:
            continue
        if looks_like_product_page(href):
            ph, ph_headers = get_html(href)
            if not ph:
                print(f"[CRAWL] NO HTML (subpage) for {href}")
                continue

            # Extract links from the subpage
            ptitle, sub_links = extract_links(ph, href)

            # Record the subpage itself as an HTML resource
            content_hash = sha256_bytes(strip_main_text(ph).encode("utf-8"))
            change, _ = record_resource(cur, href, competitor, line, "html", ph_headers, content_hash, ptitle)
            if change in ("added","updated"):
                results.append({
                    "competitor": competitor, "line": line, "url": href,
                    "what": "Product page", "change": change,
                    "old_url": None, "archived_path": None, "archived_url": None
                })

            # --- Discover PDFs on the subpage (absolute + relative) + anchor href PDFs ---
            sub_pdf_candidates = set()

            # Absolute URLs on subpage
            for m in re.finditer(r'https?://[^\s"\'<>]+\.pdf(?:\?[^\s"\'<>]*)?', ph, re.I):
                sub_pdf_candidates.add(urljoin(href, m.group(0)))

            # Relative URLs on subpage
            for m in re.finditer(r'(?:(?:\./|\../|/)[^"\'<>\s]+\.pdf(?:\?[^\s"\'<>]*)?)', ph, re.I):
                sub_pdf_candidates.add(urljoin(href, m.group(0)))

            # Anchor href PDFs on subpage
            for sub_href, _ in sub_links:
                if is_pdf(sub_href):
                    sub_pdf_candidates.add(sub_href)

            if sub_pdf_candidates:
                print(f"[CRAWL:SUB] PDFs on {href} -> {len(sub_pdf_candidates)} candidates")
                for i, cand in enumerate(sorted(list(sub_pdf_candidates))[:20]):
                    print(f"   [SUB PDF CAND {i+1:02d}] {cand}")

            # Process subpage PDF candidates (allow ARCHIVE_ALL_PDFS override)
            for pdf_url in sorted(sub_pdf_candidates):
                if pdf_url in seen:
                    continue
                seen.add(pdf_url)

                sub_text = ""  # anchor text on subpage typically not needed

                # Accept if override or pattern-match
                if not archive_all:
                    if not (PDF_PATTERNS.search(pdf_url) or PDF_PATTERNS.search(sub_text)):
                        continue

                # HEAD -> fallback GET for hash/headers
                h2 = head(pdf_url)
                dl_hash2, dl_headers2 = (None, h2)
                if h2 is None or not (h2.get("ETag") or h2.get("Last-Modified")):
                    dl_hash2, dl_headers2 = get_pdf_hash(pdf_url)

                # Classify (fallback to Brochure under override)
                doc_kind2 = classify_pdf(pdf_url + " " + sub_text)
                if not doc_kind2:
                    doc_kind2 = "Brochure" if archive_all else None
                if not doc_kind2:
                    continue

                ch2, prev_row2 = record_resource(cur, pdf_url, competitor, line, "pdf", dl_headers2, dl_hash2, sub_text)
                if ch2 in ("added", "updated"):
                    # Download & archive
                    pdf_resp2 = safe_request("GET", pdf_url)
                    pdf_bytes2 = pdf_resp2.content if pdf_resp2 else None
                    if pdf_bytes2 is None:
                        # Retry once for transient issues
                        pdf_resp2 = safe_request("GET", pdf_url)
                        pdf_bytes2 = pdf_resp2.content if pdf_resp2 else None

                    disp_name2 = beautify_filename(pdf_url or "document.pdf")
                    sha_now2 = sha256_bytes(pdf_bytes2) if pdf_bytes2 else (dl_hash2 or "nohash")

                    archived_path2 = None
                    archived_url2  = None
                    if pdf_bytes2:
                        archived_path2 = archive_pdf(competitor, line, pdf_url, pdf_bytes2, disp_name2, sha_now2)
                        if GITHUB_REPOSITORY:
                            archived_url2 = build_github_raw_url(GITHUB_REPOSITORY, GITHUB_REF_NAME, archived_path2)

                    print(f"[ARCHIVE:SUB] {competitor} | {line} | {disp_name2} -> {archived_path2 or 'NO BYTES'}")

                    results.append({
                        "competitor": competitor,
                        "line": line,
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
    # Print seed summary for visibility
    print("[SEEDS] Starting crawl across competitors/lines")
    for competitor, lines in URLS_CONF.items():
        for line in LINES_ORDER:
            urls = lines.get(line, []) or []
            for url in urls:
                print(f"[SEED] {competitor} | {line} | {url}")
                evs = crawl_seed(cur, competitor, line, url)
                events.extend(evs)
                # Politeness delay
                time.sleep(0.3)
    print(f"[SEEDS] Crawl finished with {len(events)} events")
    return events


# -------------------
# Pivot for email table
# -------------------
def pivot_for_table(all_events):
    competitors = COMPETITOR_COLS[:]  # fixed order you provided
    extras = [e["competitor"] for e in all_events if e["competitor"] not in competitors]
    competitors += [c for c in sorted(set(extras))]

    table = {line: {c: [] for c in competitors} for line in LINES_ORDER}
    na_map = build_na_map()

    for e in all_events:
        c, line, what, change = e["competitor"], e["line"], e["what"], e["change"]
        if what in ("Spec Sheet","Brochure","Data Sheet"):
            if change == "updated":
                old_href  = e.get("archived_url") or e.get("old_url") or e["url"]
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
    return competitors, table, na_map


# -------------------
# Icons for product lines
# -------------------
def line_icon_name(line: str):
    m = {
        "Door Type":      ("doortype.png",     os.path.join("assets", "doortype.png")),
        "Undercounter":   ("undercounter.png", os.path.join("assets", "undercounter.png")),
        "Prep Washer":    ("prepwasher.png",   os.path.join("assets", "prepwasher.png")),
        "Rack Conveyor":  ("rackconveyor.png", os.path.join("assets", "rackconveyor.png")),
        "Flight Type":    ("flighttype.png",   os.path.join("assets", "flighttype.png")),
    }
    return m.get(line, ("", ""))


# -------------------
# Email builder (HTML)
# -------------------
def compose_email(all_events):
    """
    Build the subject + HTML body:
    - Same fixed column width for every column (table-layout: fixed)
    - Shade FIRST column same as header, show icon above product-line text
    - Shade cells with updates using EMAIL_UPDATE_BG
    - Show 'N/A' if urls.yaml has [] for that competitor+line
    - Wrap long bullets/URLs within each cell
    """
    if all_events:
        comps = sorted({e["competitor"] for e in all_events})
        subject = "Daily WW Competitor monitor – " + ", ".join(comps)
    else:
        subject = "Daily WW Competitor monitor – No update"

    competitors, table, na_map = pivot_for_table(all_events)

    wrap_css = (
        "white-space: normal; "
        "word-break: break-word; "
        "overflow-wrap: anywhere;"
    )
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

        # First column: shaded + icon above text
        cid, _ = line_icon_name(line)

        icon_html = (
            f'<img src="cid:{cid}" alt="{line}" width="48" height="48" style="display:block; margin:0 0 4px 0;">'
            if cid else ""
        )

        html.append(
            f"<td style='font-weight:600; width:{EMAIL_COL_WIDTH}; {wrap_css} "
            f"background:{EMAIL_HEADER_BG}; color:{EMAIL_HEADER_FG}; padding:6px 8px; text-align:left;'>"
            f"{icon_html}{line}"
            f"</td>"
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
            "name": cid,                  # attachment filename
            "contentId": cid,             # must match cid in cid:...
            "isInline": True,             # inline display
            "contentBytes": base64.b64encode(data).decode("utf-8"),
            "contentType": "image/png"
        })
    return attachments

def send_via_graph(subject, html_body):
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

    send_url = f"https://graph.microsoft.com/v1.0/users/{MAIL_FROM}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": f"<html><body>{html_body}</body></html>"  # ← wrap here
            },
            "toRecipients": [{"emailAddress": {"address": addr.strip()}}
                             for addr in MAIL_TO.split(",") if addr.strip()]
        },
        "saveToSentItems": "true"
    }

    if inline_attachments:
        payload["message"]["attachments"] = inline_attachments

    h = {"Authorization": f"Bearer {access_token}", "Content-Type":"application/json"}
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

    # Add each file explicitly
    for p in paths:
        os.system(f'git add "{p}"')

    # Commit and push (ignore failure if nothing changed)
    commit_rc = os.system(f'git commit -m "{message}"')
    if commit_rc != 0:
        print("[ARCHIVE] Nothing to commit (or commit failed).")
    push_rc = os.system('git push')
    if push_rc != 0:
        print("[ARCHIVE] Push failed (check workflow permissions).")
    else:
        print("[ARCHIVE] Pushed archive commit.")


# -------------------
# Preview Test
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
# Main
# -------------------
def main():
    con = init_db()
    cur = con.cursor()

    use_samples   = os.getenv("SAMPLE_EVENTS") == "1"
    force_preview = os.getenv("WRITE_PREVIEW") == "1"

    print(f"[DEBUG] BOOTSTRAP_ARCHIVE={os.getenv('BOOTSTRAP_ARCHIVE')} "
          f"SAMPLE_EVENTS={os.getenv('SAMPLE_EVENTS')} WRITE_PREVIEW={os.getenv('WRITE_PREVIEW')} "
          f"ARCHIVE_ALL_PDFS={os.getenv('ARCHIVE_ALL_PDFS')} LOG_LINKS={os.getenv('LOG_LINKS')}")

    # --- BOOTSTRAP: crawl & archive everything, push, and exit (no email) ---
    if BOOTSTRAP and not use_samples:
        print("[BOOTSTRAP] Starting full archive …")
        all_events = crawl_all(cur)

        # Persist any resource changes made during crawl (resources table)
        con.commit()

        # commit/push archived PDFs
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
                INSERT INTO events(ts, competitor, line, url, what, change, archived_path, archived_url)
                VALUES(?,?,?,?,?,?,?,?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                e["competitor"], e["line"], e["url"], e["what"], e["change"],
                e.get("archived_path"), e.get("archived_url")
            ))
        con.commit()

    subject, body = compose_email(all_events)

    # Commit/push any archived PDFs we just saved
    archived_files = [e["archived_path"] for e in all_events if e.get("archived_path")]
    if archived_files:
        git_commit_and_push([p for p in archived_files if p], "chore: archive PDFs for today")

    # Write preview in sample mode (or if forced)
    if use_samples or force_preview:
        write_preview_file(subject, body, "preview.html")

    if SEND_MODE.upper() == "GRAPH":
        # With no credentials or in SAMPLE mode, just print (no send)
        if not (GRAPH_TENANT and GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET) or use_samples:
            print("Graph credentials not set or SAMPLE mode enabled. Skipping send.")
            print("=== SUBJECT ==="); print(subject)
            print("=== HTML BODY ==="); print(body)
            return
        # Real send via Graph
        send_via_graph(subject, body)
    else:
        # SMTP fallback
        if use_samples:
            print("SAMPLE mode with SMTP selected—printing only.")
            print("=== SUBJECT ==="); print(subject)
            print("=== HTML BODY ==="); print(body)
            return
        # Real send via SMTP
        send_via_smtp(subject, body)

   
# -------------------
# Entry point
# -------------------
if __name__ == "__main__":
    main()
