
# monitor_warewashing.py
# Python 3.11+
# Features:
# - Loads config from ./config/*.yaml
# - Crawls category and product pages, detects material changes (content hash)
# - Monitors Spec/Data Sheet & Brochure PDFs (broadened patterns incl. cut sheet, tech data, etc.)
# - Beautifies PDF file names for email display
# - Builds a 5×N HTML table (rows = product lines; columns = fixed competitor order)
# - Shades FIRST column (Product Line) same as header and shows a small icon (CID)
# - Archives PDFs to ./archive and commits/pushes them (stable links in email)
# - Sends via Microsoft Graph (client credentials), attaches icons inline
# - SQLite state to remember previous hashes/headers
# - Sample mode & preview.html writer for test workflow
# - Bootstrap mode to archive ALL PDFs once and exit (no email)
# - Added cross-host PDF discovery and raw-HTML PDF extraction

import os, re, sys, sqlite3, hashlib, json, time, base64
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, unquote
import requests
from bs4 import BeautifulSoup

# -------------------
# Small debug helper
# -------------------
def dbg(msg: str):
    """Lightweight debug print (set DEBUG_LOG=0 to silence)."""
    if os.getenv("DEBUG_LOG", "1") == "1":
        print(msg)

# -------------------
# Config loading (YAML)
# -------------------
def load_yaml(path):
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")
COMP_CONF  = load_yaml(os.path.join(CONFIG_DIR, "competitors.yaml"))
URLS_CONF  = load_yaml(os.path.join(CONFIG_DIR, "urls.yaml"))
STYLE_CONF = load_yaml(os.path.join(CONFIG_DIR, "styling.yaml"))

# Safe fallbacks if keys are missing from competitors.yaml
DEFAULT_COMPETITORS = [
    "Champion", "Jackson", "Meiko", "CMA", "Noble", "ADS", "Moyer Diebel", "Douglas", "LVO"
]
DEFAULT_LINES = ["Door Type", "Undercounter", "Prep Washer", "Rack Conveyor", "Flight Type"]

COMPETITOR_COLS = COMP_CONF.get("competitors", DEFAULT_COMPETITORS)
LINES_ORDER     = COMP_CONF.get("lines", DEFAULT_LINES)

DOOR, UNDER, PREP, RACK, FLIGHT = "Door Type", "Undercounter", "Prep Washer", "Rack Conveyor", "Flight Type"

# -------------------
# Email styling defaults (in case keys are missing)
# -------------------
EMAIL_STYLE       = STYLE_CONF.get("email", {})
EMAIL_FONT_FAMILY = EMAIL_STYLE.get("font_family", "Segoe UI, Arial, sans-serif")
EMAIL_FONT_SIZE   = EMAIL_STYLE.get("font_size", "14px")
EMAIL_HEADER_BG   = EMAIL_STYLE.get("header_bg", "#f3f3f3")
EMAIL_HEADER_FG   = EMAIL_STYLE.get("header_fg", "#000000")
EMAIL_BODY_BG     = EMAIL_STYLE.get("body_bg", "#ffffff")
EMAIL_BORDER_CLR  = EMAIL_STYLE.get("border_color", "#dddddd")
EMAIL_CELL_PAD    = EMAIL_STYLE.get("cell_padding", "6")
EMAIL_COL_WIDTH   = EMAIL_STYLE.get("column_width", "180px")  # same width for all columns
EMAIL_UPDATE_BG   = EMAIL_STYLE.get("update_bg", "#FFFFE0")   # cells with updates

# -------------------
# Archiving configuration
# -------------------
ARCHIVE_DIR        = os.getenv("ARCHIVE_DIR", "archive")
GITHUB_REPOSITORY  = os.getenv("GITHUB_REPOSITORY", "")   # e.g., "jaehankim-hobart/ww-competitor-monitor"
GITHUB_REF_NAME    = os.getenv("GITHUB_REF_NAME", "main") # branch name (Actions sets this)
BOOTSTRAP          = os.getenv("BOOTSTRAP_ARCHIVE") == "1"

# -------------------
# Build an N/A matrix
# -------------------
def build_na_map():
    """
    Returns na_map[line][competitor] = True if urls.yaml has an explicit empty list [] for that competitor+line.
    We then render 'N/A' in the table for that cell.
    """
    na = {line: {c: False for c in COMPETITOR_COLS} for line in LINES_ORDER}
    for comp in COMPETITOR_COLS:
        lines = URLS_CONF.get(comp, {})
        for line in LINES_ORDER:
            urls = lines.get(line, None)
            if isinstance(urls, list) and len(urls) == 0:
                na[line][comp] = True
    return na

# -------------------
# HTML helper (real <a> tag)
# -------------------
def a(href: str, label: str) -> str:
    return f'<a href="{href}">{label}</a>'

# -------------------
# Label helpers
# -------------------
def display_url_label(href: str, max_len: int = 60) -> str:
    """
    Build a compact, human-readable label for a URL:
      domain/last-path-segment, decoded, with -/_ -> spaces, and middle-ellipsis if too long.
    """
    try:
        u = urlparse(href)
        path = (u.path or "").rstrip("/")
        last = path.split("/")[-1] if path else ""
        if last:
            label = f"{u.netloc}/{last}"
        else:
            label = u.netloc or href
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
    name = re.sub(r"\bbrochure|sales\s*sheet|sell\s*sheet|flyer\b", "Brochure", name, flags=re.I)
    name = re.sub(r"\brev\.?\s*0?(\d{1,2})[ \-_/]?(\d{4})\b", r"Rev. \1/\2", name, flags=re.I)
    name = re.sub(r"\s{2,}", " ", name).strip()
    words = []
    for w in name.split(" "):
        if w.isupper() and len(w) <= 5:
            words.append(w)
        elif w.upper() in ACRONYM_KEEP:
            words.append(w.upper())
        elif re.search(r"\d", w) and re.search(r"[A-Za-z]", w):
            words.append(w)  # mixed token like 201HT
        else:
            words.append(w.capitalize())
    name = " ".join(words)
    name = re.sub(r"\s+(Spec Sheet|Data Sheet|Brochure)\b", r" – \1", name)
    return name

# -------------------
# PDF classification (broadened)
# -------------------
PDF_PATTERNS = re.compile(
    r"(spec(?:ification)?[\s\-]?sheet|specsheet|data[\s\-]?sheet|datasheet|"
    r"cut[\s\-]?sheet|cutsheet|sell[\s\-]?sheet|sales[\s\-]?sheet|"
    r"product[\s\-]?sheet|product[\s\-]?data|technical[\s\-]?data|tech[\s\-]?data|"
    r"brochure|flyer)",
    re.I
)

def classify_pdf(text_or_url: str):
    """Return 'Spec Sheet' | 'Data Sheet' | 'Brochure' or None."""
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
        kind TEXT,           -- html | pdf
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
        what TEXT,           -- Spec Sheet | Brochure | Product page | Data Sheet
        change TEXT,         -- added | updated
        archived_path TEXT,  -- repo path if we archived a copy
        archived_url  TEXT   -- raw GitHub URL to archived copy
    )
    """)
    con.commit()
    return con

def get_existing_resource(cur, url):
    cur.execute("SELECT url, competitor, line, kind, last_modified, etag, hash, title FROM resources WHERE url=?", (url,))
