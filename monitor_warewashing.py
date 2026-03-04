# monitor_warewashing.py
# Python 3.11+
# Features:
# - Loads config from ./config/*.yaml
# - Crawls category and product pages, detects material changes (content hash)
# - Monitors only Spec/Data Sheet & Brochure PDFs; ignores manuals/others
# - Emits events with old→new links for updated PDFs
# - Beautifies PDF file names for email display
# - Builds a 5×N HTML table (rows = product lines; columns = fixed competitor order)
# - Sends email via Microsoft Graph using app (client credentials)
# - SQLite state to remember previous hashes/headers

import os, re, sys, sqlite3, hashlib, json, time
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

# Safe fallbacks in case keys are missing from competitors.yaml
DEFAULT_COMPETITORS = [
    "Champion", "Jackson", "Meiko", "CMA", "Noble", "ADS", "Moyer Diebel", "Douglas", "LVO"
]
DEFAULT_LINES = ["Door Type", "Undercounter", "Prep Washer", "Rack Conveyor", "Flight Type"]

COMPETITOR_COLS = COMP_CONF.get("competitors", DEFAULT_COMPETITORS)
LINES_ORDER     = COMP_CONF.get("lines", DEFAULT_LINES)

DOOR, UNDER, PREP, RACK, FLIGHT = "Door Type", "Undercounter", "Prep Washer", "Rack Conveyor", "Flight Type"


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

def a(href: str, label: str) -> str:
    """HTML anchor helper."""
    return f'<a href="{href}">{label}</a>'

def pivot_for_table(all_events):
    """
    Convert flat event list into table[line][competitor] = [bullets...]
    Also return an NA map for cells that should display 'N/A'.
    """
    competitors = COMPETITOR_COLS[:]  # fixed order
    # If any unexpected competitor names show up, append them (rare)
    extras = [e["competitor"] for e in all_events if e["competitor"] not in competitors]
    competitors += [c for c in sorted(set(extras))]

    # Table of bullets and NA map
    table = {line: {c: [] for c in competitors} for line in LINES_ORDER}
    na_map = build_na_map()

    for e in all_events:
        c, line, what, change = e["competitor"], e["line"], e["what"], e["change"]
        if what in ("Spec Sheet","Brochure"):
            if change == "updated" and e.get("old_url"):
                label = (
                    f'{what} updated: '
                    f'{a(e["url"], beautify_filename(e["url"]))} '
                    f'(old: {a(e["old_url"], beautify_filename(e["old_url"]))} '
                    f'→ new: {a(e["url"], beautify_filename(e["url"]))})'
                )
            else:
                label = f'{what} {change}: {a(e["url"], beautify_filename(e["url"]))}'
            table[line][c].append(label)
        elif what == "Product page":
            # For pages, just show URL as the label
            label = f'Product page {change}: {a(e["url"], e["url"])}'
            table[line][c].append(label)

    return competitors, table, na_map


def compose_email(all_events):
    """
    Build the subject + HTML body:
    - Same fixed column width for every column (table-layout: fixed)
    - Shade cells with updates using #FFFFE0
    - Show 'N/A' when urls.yaml lists [] for that competitor+line
    - Show 'No Update' when no events and the line is applicable
    """
    if all_events:
        comps = sorted({e["competitor"] for e in all_events})
        subject = "Daily WW Competitor monitor – " + ", ".join(comps)
    else:
        subject = "Daily WW Competitor monitor – No update"

    competitors, table, na_map = pivot_for_table(all_events)
    st = STYLE_CONF["email"]

    html = []
    html.append(
        f"<div style='font-family:{st['font_family']}; font-size:{st['font_size']}; background:{st['body_bg']}'>"
    )
    html.append(f"<p><strong>Date:</strong> {datetime.now().strftime('%Y-%m-%d')}</p>")
    html.append(
        f"<table border='1' cellpadding='{st['cell_padding']}' cellspacing='0' "
        f"style='border-collapse:collapse; width:100%; border-color:{st['border_color']}; table-layout:fixed;'>"
    )

    # Header row (every column same width, including Product Line)
    html.append("<thead><tr>")
    for col in (["Product Line"] + competitors):
        html.append(
            f"<th style='text-align:left; background:{st['header_bg']}; color:{st['header_fg']}; "
            f"width:{st['column_width']};'>{col}</th>"
        )
    html.append("</tr></thead><tbody>")

    # Rows
    for line in LINES_ORDER:
        html.append("<tr>")
        html.append(f"<td style='font-weight:600; width:{st['column_width']};'>{line}</td>")

        for c in competitors:
            items = table[line].get(c, [])
            cell_style = f"width:{st['column_width']};"
            if items:  # shade cells containing updates
                cell_style += f" background:{st['update_bg']};"

            if not items:
                if na_map.get(line, {}).get(c, False):
                    cell_html = "<em>N/A</em>"
                else:
                    cell_html = "<em>No Update</em>"
            else:
                bullets = "".join(f"<li>{it}</li>" for it in items)
                cell_html = f"<ul style='margin:0 0 0 17px; padding-left:0;'>{bullets}</ul>"

            html.append(f"<td style='{cell_style}'>{cell_html}</td>")

        html.append("</tr>")

    html.append("</tbody></table></div>")
    return subject, "\n".join(html)

# -------------------
# Constants & settings
# -------------------
REQUEST_TIMEOUT = 25
HEADERS = {"User-Agent": "WW-Competitor-Monitor/1.0 (+market intel; contact: ww-monitor@itwfeg.com)"}
DB_PATH = os.getenv("STATE_DB", "state.db")

SEND_MODE = os.getenv("SEND_MODE", "GRAPH")  # GRAPH or SMTP
MAIL_TO = os.getenv("MAIL_TO", "jaehan.kim@itwfeg.com")
MAIL_FROM = os.getenv("MAIL_FROM", "ww-monitor@itwfeg.com")

# Graph (app creds) — paste real values into GitHub Secrets after IT sends them
GRAPH_TENANT = os.getenv("GRAPH_TENANT_ID", "")
GRAPH_CLIENT_ID = os.getenv("GRAPH_CLIENT_ID", "")
GRAPH_CLIENT_SECRET = os.getenv("GRAPH_CLIENT_SECRET", "")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

PDF_PATTERNS = re.compile(r"(spec(ification)?\s*sheet|data\s*sheet|datasheet|brochure|sales\s*sheet)", re.I)

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
        change TEXT          -- added | updated
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
session = requests.Session()
session.headers.update(HEADERS)

def safe_request(method, url):
    try:
        resp = session.request(method, url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return resp
    except Exception:
        return None

def sha256_bytes(b):
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
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        text = (a.get_text(" ", strip=True) or "")
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
# Crawl logic
# -------------------
def crawl_seed(cur, competitor, line, url):
    html, headers = get_html(url)
    results = []
    if not html: return results

    title, links = extract_links(html, url)

    # Record the hub/category page itself — product page updates count as events.
    change, _ = record_resource(cur, url, competitor, line, "html", headers, sha256_bytes(strip_main_text(html).encode("utf-8")), title)
    if change == "updated":
        results.append({
            "competitor": competitor, "line": line, "url": url,
            "what": "Product page", "change": "updated", "old_url": None
        })
    elif change == "added":
        results.append({
            "competitor": competitor, "line": line, "url": url,
            "what": "Product page", "change": "added", "old_url": None
        })

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
                # If no headers, get full for hash
                if h is None or not (h.get("ETag") or h.get("Last-Modified")):
                    dl_hash, dl_headers = get_pdf_hash(href)
                doc_kind = classify_pdf(href + " " + text)
                if not doc_kind:
                    continue
                change, prev_row = record_resource(cur, href, competitor, line, "pdf", dl_headers, dl_hash, text)
                if change in ("added","updated"):
                    results.append({
                        "competitor": competitor, "line": line, "url": href,
                        "what": doc_kind, "change": change,
                        "old_url": prev_row["url"] if (prev_row and change=="updated") else None
                    })
        else:
            if looks_like_product_page(href):
                ph, ph_headers = get_html(href)
                if not ph: continue
                ptitle, _ = extract_links(ph, href)
                content_hash = sha256_bytes(strip_main_text(ph).encode("utf-8"))
                change, _ = record_resource(cur, href, competitor, line, "html", ph_headers, content_hash, ptitle)
                if change in ("added","updated"):
                    results.append({
                        "competitor": competitor, "line": line,
                        "url": href, "what": "Product page", "change": change, "old_url": None
                    })
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
# Email builder (table HTML)
# -------------------
def a(href: str, label: str) -> str:
    """HTML anchor helper (real tags)."""
    return f'<a href="{href}">{label}</a>'



def pivot_for_table(all_events):
    competitors = COMPETITOR_COLS[:]  # fixed order you provided
    # Include unexpected names (very rare) at the end so we don't lose data
    extras = [e["competitor"] for e in all_events if e["competitor"] not in competitors]
    competitors += [c for c in sorted(set(extras))]

    # Table of updates
    table = {line: {c: [] for c in competitors} for line in LINES_ORDER}

    # NA matrix based on urls.yaml
    na_map = build_na_map()

    for e in all_events:
        c, line, what, change = e["competitor"], e["line"], e["what"], e["change"]
        if what in ("Spec Sheet","Brochure"):
            if change == "updated" and e.get("old_url"):
                label = (
                    f'{what} updated: '
                    f'{a(e["url"], beautify_filename(e["url"]))} '
                    f'(old: {a(e["old_url"], beautify_filename(e["old_url"]))} '
                    f'→ new: {a(e["url"], beautify_filename(e["url"]))})'
                )
            else:
                label = f'{what} {change}: {a(e["url"], beautify_filename(e["url"]))}'
            table[line][c].append(label)
        elif what == "Product page":
            label = f'Product page {change}: {a(e["url"], e["url"])}'
            table[line][c].append(label)

    return competitors, table, na_map

# -------------------
# Senders
# -------------------
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
    send_url = f"https://graph.microsoft.com/v1.0/users/{MAIL_FROM}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": addr.strip()}} for addr in MAIL_TO.split(",") if addr.strip()]
        },
        "saveToSentItems": "true"
    }
    h = {"Authorization": f"Bearer {access_token}", "Content-Type":"application/json"}
    rr = requests.post(send_url, headers=h, json=payload, timeout=20)
    rr.raise_for_status()

def send_via_smtp(subject, html_body):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(MAIL_FROM, [a.strip() for a in MAIL_TO.split(",") if a.strip()], msg.as_string())


def sample_events_for_preview():
    # Simulated events that exercise different lines/competitors
    return [
        # Rack Conveyor – Champion spec sheet updated
        {"competitor":"Champion","line":"Rack Conveyor","url":"https://www.championindustries.com/content/spec-sheets/Rack-Conveyors/44-PRO-VHR_Electric_Rev.09-2025.pdf","what":"Spec Sheet","change":"updated","old_url":"https://www.championindustries.com/content/spec-sheets/Rack-Conveyors/44-PRO-VHR_Electric_Rev.08-2025.pdf"},
        # Rack Conveyor – Jackson brochure added
        {"competitor":"Jackson","line":"Rack Conveyor","url":"https://www.jacksonwws.com/wp-content/uploads/2026/02/RackStar_66_ER_brochure.pdf","what":"Brochure","change":"added","old_url":None},
        # Door Type – CMA product page updated
        {"competitor":"CMA","line":"Door Type","url":"https://cmadishmachines.com/product/model-180-straight/","what":"Product page","change":"updated","old_url":None},
        # Undercounter – Meiko product page added
        {"competitor":"Meiko","line":"Undercounter","url":"https://www.meiko.com/en-us/products/commercial-dishwashers/undercounter-dishwashers/fv-402-g","what":"Product page","change":"added","old_url":None},
        # Prep Washer – Douglas brochure added
        {"competitor":"Douglas","line":"Prep Washer","url":"https://www.dougmac.com/wp-content/uploads/2024/08/Product-Sheet-Bucket-Pan-Washer.pdf","what":"Brochure","change":"added","old_url":None},
        # Prep Washer – LVO product page updated
        {"competitor":"LVO","line":"Prep Washer","url":"https://www.lvomfg.com/site/product/fl36/","what":"Product page","change":"updated","old_url":None},
        # Door Type – ADS product page added
        {"competitor":"ADS","line":"Door Type","url":"https://www.americandish.com/product/upright-dish-machine-af-afc-es/","what":"Product page","change":"added","old_url":None},
        # Undercounter – Moyer Diebel spec sheet added
        {"competitor":"Moyer Diebel","line":"Undercounter","url":"https://moyerdiebel.com/content/specs/383HT_Spec_Sheet.pdf","what":"Spec Sheet","change":"added","old_url":None},
        # Flight Type – Jackson product page updated
        {"competitor":"Jackson","line":"Flight Type","url":"https://www.jacksonwws.com/products/flightstar/","what":"Product page","change":"updated","old_url":None},
    ]


# -------------------
# Main
# -------------------

def main():
    con = init_db()
    cur = con.cursor()

    use_samples = os.getenv("SAMPLE_EVENTS") == "1"
    if use_samples:
        all_events = sample_events_for_preview()
    else:
        all_events = crawl_all(cur)
        # Persist only real crawl events
        for e in all_events:
            cur.execute("INSERT INTO events(ts, competitor, line, url, what, change) VALUES(?,?,?,?,?,?)",
                        (datetime.now(timezone.utc).isoformat(), e["competitor"], e["line"], e["url"], e["what"], e["change"]))
        con.commit()

    subject, body = compose_email(all_events)

    if SEND_MODE.upper() == "GRAPH":
        # With no credentials or in SAMPLE mode, just print (no send)
        if not (GRAPH_TENANT and GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET) or use_samples:
            print("Graph credentials not set or SAMPLE mode enabled. Skipping send.")
            print("=== SUBJECT ===")
            print(subject)
            print("=== HTML BODY ===")
            print(body)
            return
        send_via_graph(subject, body)
    else:
        # SMTP fallback (optional): in sample mode, just print
        if use_samples:
            print("SAMPLE mode with SMTP selected—printing only.")
            print("=== SUBJECT ==="); print(subject)
            print("=== HTML BODY ==="); print(body)
            return
        send_via_smtp(subject, body)


# 1) Run and capture stdout
- name: Run monitor in SAMPLE mode (no send)
  env:
    SEND_MODE: GRAPH
    MAIL_FROM: "ww-monitor@itwfeg.com"
    MAIL_TO: "jaehan.kim@itwfeg.com"
    GRAPH_TENANT_ID: ${{ secrets.GRAPH_TENANT_ID }}
    GRAPH_CLIENT_ID: ${{ secrets.GRAPH_CLIENT_ID }}
    GRAPH_CLIENT_SECRET: ${{ secrets.GRAPH_CLIENT_SECRET }}
    SAMPLE_EVENTS: "1"
  run: |
    set -e
    python monitor_warewashing.py | tee monitor_output.txt

# 2) Extract HTML body
- name: Build preview.html from output
  run: |
    awk '/=== HTML BODY ===/{flag=1;next} flag{print}' monitor_output.txt > preview.html
    echo "Wrote preview.html"

# 3) Upload artifact
- name: Upload preview artifact
  uses: actions/upload-artifact@v4
  with:
    name: ww-monitor-preview
    path: preview.html
