
# monitor_warewashing.py
# Python 3.11+
# Features:
# - Loads config from ./config/*.yaml
# - Crawls category and product pages, detects material changes (content hash)
# - Monitors only Spec/Data Sheet & Brochure PDFs; ignores manuals/others
# - Beautifies PDF file names for email display
# - Builds a 5×N HTML table (rows = product lines; columns = fixed competitor order)
# - Shades FIRST column (Product Line) same as header and shows a small icon (CID)
# - Archives PDFs to ./archive and commits/pushes them (stable links in email)
# - Sends via Microsoft Graph (client credentials), attaches icons inline
# - SQLite state to remember previous hashes/headers
# - Sample mode & preview.html writer for test workflow
# - Bootstrap mode to archive ALL PDFs once and exit (no email)

import os, re, sys, sqlite3, hashlib, json, time, base64
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, unquote
import requests
from bs4 import BeautifulSoup

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
    name = re.sub(r"\bbrochure|sales\s*sheet\b", "Brochure", name, flags=re.I)
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

def classify_pdf(text_or_url):
    s = text_or_url or ""
    if re.search(r"(spec(ification)?\s*sheet|data\s*sheet|datasheet)", s, re.I):
        return "Spec Sheet"
    if re.search(r"(brochure|sales\s*sheet)", s, re.I):
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
        what TEXT,           -- Spec Sheet | Brochure | Product page
        change TEXT,         -- added | updated
        archived_path TEXT,  -- repo path if we archived a copy
        archived_url  TEXT   -- raw GitHub URL to archived copy
    )
    """)
    con.commit()
    return con

def get_existing_resource(cur, url):
    cur.execute("SELECT url, competitor, line, kind, last_modified, etag, hash, title FROM resources WHERE url=?", (url,))
    row = cur.fetchone()
    if row:
        keys = ["url","competitor","line","kind","last_modified","etag","hash","title"]
        return dict(zip(keys, row))
    return None

def record_resource(cur, url, competitor, line, kind, headers, content_hash, title):
    prev_row = get_existing_resource(cur, url)
    cur.execute("SELECT last_modified, etag, hash FROM resources WHERE url=?", (url,))
    row = cur.fetchone()
    now = datetime.now(timezone.utc).isoformat()
    last_mod = headers.get("Last-Modified") if headers else None
    etag = headers.get("ETag") if headers else None

    if row is None:
        cur.execute("""INSERT INTO resources(url, competitor, line, kind, last_modified, etag, hash, title, first_seen, last_seen)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (url, competitor, line, kind, last_mod, etag, content_hash, title, now, now))
        return "added", prev_row
    else:
        prev_mod, prev_etag, prev_hash = row
        changed = False
        if (last_mod and last_mod != prev_mod) or (etag and etag != prev_etag) or (content_hash and prev_hash and content_hash != prev_hash):
            changed = True
        if changed:
            cur.execute("""UPDATE resources SET last_modified=?, etag=?, hash=?, title=?, last_seen=?
                           WHERE url=?""",
                        (last_mod, etag, content_hash or prev_hash, title, now, url))
            return "updated", prev_row
        else:
            cur.execute("UPDATE resources SET last_seen=? WHERE url=?", (now, url))
            return None, prev_row

# -------------------
# HTTP helpers
# -------------------
REQUEST_TIMEOUT = 25
HEADERS = {"User-Agent": "WW-Competitor-Monitor/1.0 (+market intel; contact: ww-monitor@itwfeg.com)"}

session = requests.Session()
session.headers.update(HEADERS)

def safe_request(method, url):
    try:
        resp = session.request(method, url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return resp
    except Exception:
        return None

def sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256(); h.update(b); return h.hexdigest()

def get_html(url):
    r = safe_request("GET", url)
    if not r: return None, None
    return r.text, r.headers

def head(url):
    r = safe_request("HEAD", url)
    if r and r.status_code < 400:
        return r.headers
    return None

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
    body = soup.get_text(" ", strip=True)
    return " ".join(body.split())[:20000]

def looks_like_product_page(url):
    bad = ("/privacy", "/terms", "/sitemap", "/contact", "/search", "/news", "/blog", "/careers")
    if any(x in url.lower() for x in bad): return False
    return any(x in url.lower() for x in ("/product", "/products/", "/our-products", "/rack", "/door", "/flight", "/dish", "/washer", "/categories/"))

def infer_line_from_path(url):
    s = url.lower()
    if "flight" in s: return FLIGHT
    if "rack" in s or "conveyor" in s: return RACK
    if "door" in s or "upright" in s: return DOOR
    if "under" in s: return UNDER
    if any(x in s for x in ["pan","pot","rack-washer","prep"]): return PREP
    return "Unknown"

# -------------------
# PDF filter (FIX 1: ensure defined before crawl_seed uses it)
# -------------------
PDF_PATTERNS = re.compile(
    r"(spec(ification)?\s*sheet|data\s*sheet|datasheet|brochure|sales\s*sheet)",
    re.I
)

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
# Crawl logic
# -------------------
def crawl_seed(cur, competitor, line, url):
    html, headers = get_html(url)
    results = []
    if not html: return results

    title, links = extract_links(html, url)

    # Record the hub/category page itself — product page updates count as events.
    change, _ = record_resource(cur, url, competitor, line, "html",
                                headers, sha256_bytes(strip_main_text(html).encode("utf-8")), title)
    if change == "updated":
        results.append({"competitor": competitor, "line": line, "url": url, "what": "Product page", "change": "updated", "old_url": None, "archived_path": None, "archived_url": None})
    elif change == "added":
        results.append({"competitor": competitor, "line": line, "url": url, "what": "Product page", "change": "added", "old_url": None, "archived_path": None, "archived_url": None})

    # Expand within same host
    host = urlparse(url).netloc
    seen = set()
    for href, text in links:
        if href in seen: continue
        seen.add(href)
        if urlparse(href).netloc != host: continue

        if is_pdf(href):
            # Only spec/data sheet or brochure
            if PDF_PATTERNS.search(href) or PDF_PATTERNS.search(text):
                h = head(href)
                dl_hash, dl_headers = (None, h)
                if h is None or not (h.get("ETag") or h.get("Last-Modified")):
                    dl_hash, dl_headers = get_pdf_hash(href)

                doc_kind = classify_pdf(href + " " + text)
                if not doc_kind:
                    continue

                change, prev_row = record_resource(cur, href, competitor, line, "pdf", dl_headers, dl_hash, text)
                if change in ("added","updated"):
                    # Download full PDF to archive it
                    pdf_resp  = safe_request("GET", href)
                    pdf_bytes = pdf_resp.content if pdf_resp else None
                    disp_name = beautify_filename(href or text or "document.pdf")
                    sha_now   = sha256_bytes(pdf_bytes) if pdf_bytes else (dl_hash or "")

                    archived_path = None
                    archived_url  = None
                    if pdf_bytes:
                        archived_path = archive_pdf(competitor, line, href, pdf_bytes, disp_name, sha_now)
                        if GITHUB_REPOSITORY:
                            archived_url = build_github_raw_url(GITHUB_REPOSITORY, GITHUB_REF_NAME, archived_path)

                    results.append({
                        "competitor": competitor,
                        "line": line,
                        "url": href,                 # new/current site URL
                        "what": doc_kind,
                        "change": change,
                        "old_url": prev_row["url"] if (prev_row and change=="updated") else None,
                        "archived_url": archived_url,   # stable link to *new* archived copy
                        "archived_path": archived_path
                    })

        else:
            if looks_like_product_page(href):
                ph, ph_headers = get_html(href)
                if not ph: continue
                ptitle, _ = extract_links(ph, href)
                content_hash = sha256_bytes(strip_main_text(ph).encode("utf-8"))
                change, _ = record_resource(cur, href, competitor, line, "html", ph_headers, content_hash, ptitle)
                if change in ("added","updated"):
                    results.append({"competitor": competitor, "line": line, "url": href, "what": "Product page", "change": change, "old_url": None, "archived_path": None, "archived_url": None})
    return results

def crawl_all(cur):
    events = []
    for competitor, lines in URLS_CONF.items():
        for line in LINES_ORDER:
            urls = lines.get(line, []) or []
            for url in urls:
                evs = crawl_seed(cur, competitor, line, url)
                events.extend(evs)
    return events

# -------------------
# Pivot for table
# -------------------
def pivot_for_table(all_events):
    competitors = COMPETITOR_COLS[:]  # fixed order you provided
    extras = [e["competitor"] for e in all_events if e["competitor"] not in competitors]
    competitors += [c for c in sorted(set(extras))]

    table = {line: {c: [] for c in competitors} for line in LINES_ORDER}
    na_map = build_na_map()

    for e in all_events:
        c, line, what, change = e["competitor"], e["line"], e["what"], e["change"]
        if what in ("Spec Sheet","Brochure"):
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
# Map product line to inline icon (CID + local file path)
# -------------------
def line_icon_name(line: str) -> tuple[str, str]:
    m = {
        "Door Type":      ("doortype.png",     os.path.join("assets", "doortype.png")),
        "Undercounter":   ("undercounter.png", os.path.join("assets", "undercounter.png")),
        "Prep Washer":    ("prepwasher.png",   os.path.join("assets", "prepwasher.png")),
        "Rack Conveyor":  ("rackconveyor.png", os.path.join("assets", "rackconveyor.png")),
        "Flight Type":    ("flighttype.png",   os.path.join("assets", "flighttype.png")),
    }
    return m.get(line, ("", ""))

# -------------------
# Email builder (table HTML) with shaded first column + icon + robust wrapping
# -------------------
def compose_email(all_events):
    """
    Build the subject + HTML body:
    - Same fixed column width for every column (table-layout: fixed)
    - Shade FIRST column same as header, show icon above product-line text with a hard <br>
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

        # First column: shaded + icon above text + hard line break (FIX 2: real <img> tag)
        cid, _ = line_icon_name(line)
        icon_html = (
            f'<img src="cid:{cid}" alt="{line}" width="48" height="48" '
            f'style="display:block; margin:0 0 4px 0;">'
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
# Senders
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
    r = requests.post(token_url, data=data, timeout=20)
    r.raise_for_status()
    access_token = r.json()["access_token"]

    inline_attachments = _build_inline_attachments_for_lines()

    send_url = f"https://graph.microsoft.com/v1.0/users/{MAIL_FROM}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": addr.strip()}} for addr in MAIL_TO.split(",") if addr.strip()]
        },
        "saveToSentItems": "true"
    }
    if inline_attachments:
        payload["message"]["attachments"] = inline_attachments

    h = {"Authorization": f"Bearer {access_token}", "Content-Type":"application/json"}
    rr = requests.post(send_url, headers=h, json=payload, timeout=20)
    rr.raise_for_status()

def send_via_smtp(subject, html_body):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = MAIL_FROM
    msg["To"]      = MAIL_TO
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(MAIL_FROM, [a.strip() for a in MAIL_TO.split(",") if a.strip()], msg.as_string())

# -------------------
# Git commit & push for archives
# -------------------
def git_commit_and_push(paths: list[str], message: str = "chore: archive updated PDFs"):
    """
    Commit and push the given files to the current branch (uses GITHUB_TOKEN in Actions).
    No-op if 'paths' is empty or running outside GitHub Actions without creds.
    """
    if not paths:
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
    os.system(f'git commit -m "{message}" || echo "Nothing to commit"')
    os.system('git push || echo "Push failed (check workflow permissions)"')

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
    print(f"[DEBUG] BOOTSTRAP_ARCHIVE={os.getenv('BOOTSTRAP_ARCHIVE')} SAMPLE_EVENTS={os.getenv('SAMPLE_EVENTS')} WRITE_PREVIEW={os.getenv('WRITE_PREVIEW')}")

    # --- BOOTSTRAP: crawl & archive everything, push, and exit (no email) ---
    if BOOTSTRAP and not use_samples:
        print("[BOOTSTRAP] Starting full archive …")
        all_events = crawl_all(cur)
        # Persist any resource changes made during crawl (resources table)
        con.commit()
        # commit/push archived PDFs
        archived_files = [e["archived_path"] for e in all_events if e.get("archived_path")]
        if archived_files:
            git_commit_and_push(archived_files, "bootstrap: initial PDF archive")
            print(f"[BOOTSTRAP] Archived and pushed {len(archived_files)} PDFs.")
        else:
            print("[BOOTSTRAP] No PDFs discovered to archive (check seeds).")
        print("[BOOTSTRAP] Done. Exiting without sending email.")
        return

    # --- Daily / Sample runs ---
    if use_samples:
        all_events = sample_events_for_preview()
    else:
        all_events = crawl_all(cur)
        # Persist only real crawl events (not needed for sample)
        for e in all_events:
            cur.execute("INSERT INTO events(ts, competitor, line, url, what, change, archived_path, archived_url) VALUES(?,?,?,?,?,?,?,?)",
                        (datetime.now(timezone.utc).isoformat(), e["competitor"], e["line"], e["url"], e["what"], e["change"], e.get("archived_path"), e.get("archived_url")))
        con.commit()

    subject, body = compose_email(all_events)

    # Commit/push any archived PDFs we just saved (daily runs)
    archived_files = [e["archived_path"] for e in all_events if e.get("archived_path")]
    if archived_files:
        git_commit_and_push(archived_files, "chore: archive PDFs for today")

    # Write preview in sample mode (for test workflow)
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
