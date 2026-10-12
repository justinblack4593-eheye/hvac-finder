"""
HVAC Lead Finder v4.3 - several planning commissions -> Claude lead cards -> one email.

Run normally:   py lead_finder.py
Test the sites: py lead_finder.py --check   (no AI, no email - just shows what each site finds)

Add a site = add one entry to SITES below.
Sites that block automation: save their PDFs into manual/emmet, manual/bearcreek or
manual/traversecity (put the meeting date in the file name, e.g. 2026-11-05 agenda.pdf).
"""
import email
import hashlib
import imaplib
import json
import os
import re
import smtplib
import sys
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import pymupdf
import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ SITES
# listing: page that links to the meeting PDFs
# include: which links belong to planning commission meetings
# anchor:  which of those START a meeting (usually the agenda)
# exclude: links to skip
SITES = [
    {
        "id": "garfield",
        "name": "Garfield Twp (Grand Traverse)",
        "listing": "https://www.garfieldmi.gov/boards-and-commissions/planning-commission/",
        "include": r"pc[-_ ]?(agenda|handout)",
        "anchor": r"pc[-_ ]?agenda",
        "exclude": r"minutes|video",
    },
    {
        "id": "resort",
        "name": "Resort Twp (Emmet)",
        "listing": "https://www.resorttownship.gov/boards-committees/planning-commission/2026-minutes-agendas-planning/",
        "include": r"agenda|packet|case",
        "anchor": r"agenda",
        "exclude": r"minutes",
    },
    {
        "id": "acme",
        "name": "Acme Twp (Grand Traverse)",
        "listing": "https://www.acmetownship.org/current-meeting-minutes.html",
        "include": r".",
        "anchor": r"pc_packet|pc_approved",
        "exclude": r"tb_|_tb|township_board|p_t_|parks|zba|cip_|zo_|spr_|personnel",
    },
]

# ------------------------------------------------------------------ SETTINGS
API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
MODEL = "claude-haiku-5-5"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"

GMAIL_ADDRESS = (os.environ.get("GMAIL_ADDRESS") or "").strip()
GMAIL_APP_PASSWORD = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
EMAIL_TO = (os.environ.get("EMAIL_TO") or GMAIL_ADDRESS).strip()

STATE_FILE = Path("leads_state.json")

# Newspaper public-notice alerts (mipublicnotices.com) that arrive in your AI Gmail inbox.
# Covers boards whose own sites block automation (Traverse City, Emmet County, Bear Creek...).
ALERT_SENDERS = ("mipublicnotices", "publicnotice", "michiganpress")  # matched against the From line
ALERT_DAYS = 14

# Drop folders for sites that block automated access. Save their PDFs here by hand.
MANUAL_DIR = Path("manual")
MANUAL_SOURCES = {
    "emmet": "Emmet County PC",
    "bearcreek": "Bear Creek Twp (Emmet)",
    "traversecity": "Traverse City PC",
}
PDF_DIR = Path("pdfs")
FIRST_RUN_COUNT = 2   # first time a site is seen: report its newest 2 meetings, skip older ones
MAX_PER_SITE = 3      # after that: at most 3 new meetings per site per run
MAX_CHARS = 120_000   # keeps each request under the 100K-token price jump
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PROMPT = """You are a lead analyst for a commercial HVAC contractor in northern Michigan.
Below are documents from one {site} planning commission meeting (agenda, packet, staff reports, site plans).
Each document starts with a marker like [D1], [D2].
List every development project. Use the packet/handouts to fill in details.
For each project give exactly these lines, with a blank line between projects:
- Key: (see KNOWN PROJECTS rules below)
- Project: name
- Case: case/PD number if stated, else "none"
- Location: street address or area
- Type: (retail, apartments, restaurant, office, industrial, etc.)
- Size: square feet and/or number of units; "not stated" only if truly absent
- Buildings: number of buildings if stated, else "not stated"
- Developer/Applicant: name if stated
- Engineer/Architect: firm and contact person if stated (check site plan title blocks and staff reports)
- Contractor/Builder: general contractor or builder if stated, else "not stated"
- Stage: (conceptual, site plan review, special use permit, approved, etc.)
- Next step/date: the next scheduled action and its date if stated, else "not stated"
- HVAC value: High / Medium / Low, with one short reason
- Documents: the markers of every document that discusses this project, e.g. [D1] [D3]
Skip routine items (minutes, reports, public comment, ordinance text changes with no specific project).
If these are newspaper legal notices, ignore foreclosures, probate, budgets, elections and anything
that is not a specific building or land development project.
If there are no projects, reply "No projects." Plain text only, no tables, no markdown.

KNOWN PROJECTS from earlier meetings of this commission. If a project in these documents is the
same development as one listed (same site or address, even if renamed, amended, or at a different
stage), use its exact Key. Otherwise create a new Key: lowercase words joined by hyphens,
like 4040-creekside-apartments.
{known}

DOCUMENTS:
"""

ALERT_SITE = {"id": "notices", "name": "Newspaper public notices", "email": True}

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


# ------------------------------------------------------------------ HELPERS
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": [], "projects": {}, "sites_started": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def robots_ok(url):
    """Respect each site's robots.txt."""
    p = urlparse(url)
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt", headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return True
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp.can_fetch(HEADERS["User-Agent"], url)
    except Exception:
        return True


def fname(href):
    return unquote(urlparse(href).path.rstrip("/").split("/")[-1])


def is_doc(href):
    h = href.lower()
    return (urlparse(h).path.endswith(".pdf") or "/download/" in h or "viewfile" in h
            or "documentcenter/view" in h or "/wp-content/uploads/" in h)


def safe_date(y, m, d):
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def parse_date(text):
    t = text.lower()
    m = re.search(r"(20\d{2})[-_/.](\d{1,2})[-_/.](\d{1,2})", t)
    if m:
        return safe_date(m.group(1), m.group(2), m.group(3))
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})", t)
    if m:
        return safe_date(m.group(3), MONTHS[m.group(1)], m.group(2))
    m = re.search(r"(?<!\d)(\d{1,2})[-_/.](\d{1,2})[-_/.](20\d{2}|\d{2})(?!\d)", t)
    if m:
        y = int(m.group(3))
        return safe_date(y + 2000 if y < 100 else y, m.group(1), m.group(2))
    m = re.search(r"(?<!\d)(\d{1,2})[-_/](\d{1,2})(?![\d\-_/])", t)
    if m:  # month-day with no year, like "pc-agenda-10-14"
        today = date.today()
        d = safe_date(today.year, m.group(1), m.group(2))
        if d and d > today + timedelta(days=60):
            d = safe_date(today.year - 1, m.group(1), m.group(2))
        return d
    return None


def find_meetings(site):
    resp = requests.get(site["listing"], headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        print(f"  ! listing page returned status {resp.status_code}")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    groups, current, used = [], None, set()
    for a in soup.find_all("a", href=True):
        href = urljoin(site["listing"], a["href"]).split("#")[0]
        if href in used or not is_doc(href):
            continue
        used.add(href)
        label = a.get_text(" ", strip=True) or fname(href)
        text = f"{label} {fname(href)}"
        if not re.search(site["include"], text, re.I):
            continue
        if site.get("exclude") and re.search(site["exclude"], text, re.I):
            continue
        if re.search(site["anchor"], text, re.I):
            d = parse_date(text)
            if current and d and current["date"] == d:
                current["docs"].append((label, href))  # e.g. a revised agenda, same meeting
                continue
            title = f"Meeting {d:%b %d, %Y}" if d else label
            current = {"id": href, "date": d, "title": title, "docs": [(label, href)]}
            groups.append(current)
        elif current:
            current["docs"].append((label, href))
    groups.sort(key=lambda g: g["date"] or date.min, reverse=True)
    return groups


def manual_sites():
    """One 'site' per subfolder of manual/ (folders are created automatically)."""
    for folder in MANUAL_SOURCES:
        (MANUAL_DIR / folder).mkdir(parents=True, exist_ok=True)
    return [{"id": f"manual-{sub.name}",
             "name": MANUAL_SOURCES.get(sub.name, sub.name) + " (manual)",
             "folder": sub}
            for sub in sorted(p for p in MANUAL_DIR.iterdir() if p.is_dir())]


def find_manual(site):
    """PDFs you saved by hand. Files with the same date in their name = one meeting."""
    groups = {}
    for f in sorted(site["folder"].glob("*.pdf")):
        d = parse_date(f.stem)
        key = d.isoformat() if d else f.name
        g = groups.setdefault(key, {"date": d, "docs": [],
                                    "title": f"Meeting {d:%b %d, %Y}" if d else f.stem})
        g["docs"].append((f.name, str(f)))
    out = []
    for g in groups.values():
        g["id"] = f"{site['id']}:" + "|".join(sorted(n for n, _ in g["docs"]))
        out.append(g)
    out.sort(key=lambda g: g["date"] or date.min, reverse=True)
    return out


def _decode(v):
    return str(make_header(decode_header(v or "")))


def email_text(msg):
    """Plain text of an email plus any links in it."""
    text, links = "", []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        raw = part.get_payload(decode=True) or b""
        body = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/html":
            soup = BeautifulSoup(body, "html.parser")
            links += [a["href"] for a in soup.find_all("a", href=True) if a["href"].startswith("http")]
            body = soup.get_text("\n")
        if len(body) > len(text):
            text = body
    return text, list(dict.fromkeys(links))


def find_alert_emails(site):
    """New public-notice alert emails from the last ALERT_DAYS days."""
    groups = []
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        M.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        M.select("INBOX", readonly=True)
        since = (date.today() - timedelta(days=ALERT_DAYS)).strftime("%d-%b-%Y")
        _, data = M.search(None, "SINCE", since)
        for num in data[0].split()[-300:]:
            _, hdr = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
            if not any(k in hdr[0][1].decode(errors="replace").lower() for k in ALERT_SENDERS):
                continue
            _, full = M.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(full[0][1])
            text, links = email_text(msg)
            subject = _decode(msg.get("Subject")) or "Public notice alert"
            mid = msg.get("Message-ID") or f"{subject}|{msg.get('Date')}"
            groups.append({"id": f"email:{mid}", "date": date.today(), "title": subject,
                           "docs": [(subject, links[0] if links else "(see email)")],
                           "text": f"Subject: {subject}\n\n{text}"})
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return groups


def get_groups(site):
    if site.get("email"):
        return find_alert_emails(site)
    return find_manual(site) if "folder" in site else find_meetings(site)


def download(site_id, url):
    if not url.startswith("http"):
        return Path(url)  # a file from a manual folder
    PDF_DIR.mkdir(exist_ok=True)
    h = hashlib.sha1(url.encode()).hexdigest()[:10]
    path = PDF_DIR / f"{site_id}_{h}.pdf"
    if not path.exists():
        data = requests.get(url, headers=HEADERS, timeout=120).content
        if not data.startswith(b"%PDF"):
            raise RuntimeError("link did not return a PDF")
        path.write_bytes(data)
    return path


def extract_text(path):
    cache = path.with_suffix(".txt")
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    with pymupdf.open(path) as doc:
        text = "\n".join(page.get_text() for page in doc)
    cache.write_text(text, encoding="utf-8")
    return text


TOKENS = {"in": 0, "out": 0}


def tidy(text):
    """Drop junk lines (dimension labels, lone numbers) that site plans produce."""
    keep = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if len(line) >= 3 and sum(c.isalpha() for c in line) >= 3:
            keep.append(line)
    return "\n".join(keep)


def ask_ai(full_prompt):
    r = requests.post(
        CLAUDE_URL,
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 8000,
              "messages": [{"role": "user", "content": full_prompt}]},
        timeout=240,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    data = r.json()
    usage = data.get("usage", {})
    TOKENS["in"] += usage.get("input_tokens", 0)
    TOKENS["out"] += usage.get("output_tokens", 0)
    print(f"    tokens: {usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out")
    if data.get("stop_reason") == "max_tokens":
        print("  ! warning: output was cut off - some projects may be missing")
    return "\n".join(b["text"] for b in data["content"] if b.get("type") == "text").strip()


def clean(line):
    return re.sub(r"^[\-\*\d\.\)\s]+", "", line.strip())


def field(block, name):
    for line in block.splitlines():
        s = clean(line)
        if s.lower().startswith(name.lower() + ":"):
            return s.split(":", 1)[1].strip()
    return ""


def filter_cards(cards, site_id, title, order, projects):
    """Keep projects that are new, or newer than what we've already reported."""
    kept = []
    for block in re.split(r"\n\s*\n", cards):
        if not field(block, "Project"):
            continue
        key = field(block, "Key").lower().strip()
        if not key:
            key = re.sub(r"[^a-z0-9]+", "-", field(block, "Project").lower()).strip("-")
        pkey = f"{site_id}:{key}"
        prev = projects.get(pkey)
        if prev and prev["order"] >= order:
            continue
        tag = f"UPDATE - previously: {prev['stage']} ({prev['meeting']})" if prev else "NEW"
        body = "\n".join(l for l in block.strip().splitlines()
                         if not clean(l).lower().startswith("key:"))
        kept.append(f"[{tag}]\n{body}")
        projects[pkey] = {"name": field(block, "Project"), "stage": field(block, "Stage"),
                          "meeting": title, "order": order}
    return kept


def fill_links(text, docs):
    def repl(match):
        n = int(match.group(1))
        if 1 <= n <= len(docs):
            label, href = docs[n - 1]
            return f"\n    {label}: {href}"
        return ""
    return re.sub(r"\[D(\d+)\]", repl, text)


def send_email(sections, count):
    msg = EmailMessage()
    msg["Subject"] = f"HVAC Leads - {count} meeting(s) - {date.today():%b %d, %Y}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = EMAIL_TO
    msg.set_content("New planning commission leads\n\n"
                    + ("\n\n" + "=" * 50 + "\n\n").join(sections))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            smtp.send_message(msg)
        return
    except Exception as e:
        print(f"  port 465 failed ({e}), trying 587...")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


# ------------------------------------------------------------------ MODES
def check_sites():
    """Show what each site finds. No AI, no email, nothing saved."""
    for site in SITES + manual_sites() + [ALERT_SITE]:
        print("=" * 60)
        print(site["name"], "|", site.get("listing") or site.get("folder") or "Gmail inbox")
        if "listing" in site and not robots_ok(site["listing"]):
            print("  BLOCKED by the site's robots.txt - leave this site out.")
            continue
        try:
            groups = get_groups(site)
        except Exception as e:
            print(f"  ! failed: {e}")
            continue
        print(f"  {len(groups)} meetings found")
        for g in groups[:4]:
            print(f"   - {g['title']}: {len(g['docs'])} doc(s), first: {g['docs'][0][0]}")


def main():
    print("Lead finder v4.3 (sites + public-notice emails + drop folders)")
    missing = [n for n, v in [("ANTHROPIC_API_KEY", API_KEY), ("GMAIL_ADDRESS", GMAIL_ADDRESS),
                              ("GMAIL_APP_PASSWORD", GMAIL_APP_PASSWORD)] if not v]
    if missing:
        print("Not set:", ", ".join(missing), "- run setx, then File > Exit and reopen VS Code.")
        return

    state = load_state()
    seen = set(state["seen"])
    projects = state["projects"]
    started = set(state["sites_started"])
    sections, newly_seen, newly_started, meeting_count = [], set(), set(), 0

    for site in SITES + manual_sites() + [ALERT_SITE]:
        manual = "folder" in site or site.get("email")
        print(f"\n{site['name']}")
        if not manual and not robots_ok(site["listing"]):
            print("  skipped: robots.txt blocks automated access")
            continue
        try:
            groups = get_groups(site)
        except Exception as e:
            print(f"  ! failed to read: {e}")
            continue
        first = (not manual) and site["id"] not in started
        unseen = [g for g in groups if g["id"] not in seen]
        todo = unseen[:FIRST_RUN_COUNT if first else MAX_PER_SITE]
        print(f"  {len(groups)} meetings, {len(unseen)} unseen, processing {len(todo)}"
              + (" (first run for this site)" if first else ""))

        site_ok = True
        for g in reversed(todo):  # oldest first, so later meetings show as updates
            print(f"  {g['title']} ({len(g['docs'])} docs)...", flush=True)
            parts = [f"[D1] {g['title']}\n{g['text']}"] if "text" in g else []
            for i, (label, href) in enumerate([] if "text" in g else g["docs"], start=1):
                try:
                    parts.append(f"[D{i}] {label}\n" + tidy(extract_text(download(site["id"], href))))
                except Exception as e:
                    print(f"    skipped doc '{label}': {e}")
            if not parts:
                site_ok = False
                continue
            known = "\n".join(f"{k.split(':', 1)[1]} | {v['name']} | last stage: {v['stage']}"
                              for k, v in projects.items() if k.startswith(site["id"] + ":")) or "(none yet)"
            prompt = PROMPT.replace("{site}", site["name"]).replace("{known}", known)
            try:
                cards = ask_ai(prompt + "\n\n".join(parts)[:MAX_CHARS])
            except Exception as e:
                print(f"    ! AI failed: {e}")
                site_ok = False
                continue
            newly_seen.add(g["id"])
            order = g["date"].toordinal() if g["date"] else date.today().toordinal()
            kept = filter_cards(cards, site["id"], g["title"], order, projects)
            if not kept:
                print("    nothing new.")
                continue
            all_docs = "\n".join(f"  {label}: {href}" for label, href in g["docs"])
            sections.append(f"{site['name']} - {g['title']}\n\n" + fill_links("\n\n".join(kept), g["docs"])
                            + f"\n\nAll documents for this meeting:\n{all_docs}")
            meeting_count += 1
            print(f"    {len(kept)} project(s) to report.")

        if first and site_ok and groups:
            newly_seen.update(g["id"] for g in groups)  # older meetings count as seen
            newly_started.add(site["id"])

    if sections:
        try:
            send_email(sections, meeting_count)
            print(f"\nEmail sent to {EMAIL_TO} with {meeting_count} meeting(s).")
        except Exception as e:
            print(f"\n! email failed: {e}")
            return
    else:
        print("\nNo new leads. No email sent.")

    print(f"Run total tokens: {TOKENS['in']:,} in / {TOKENS['out']:,} out")
    state["seen"] = sorted(seen | newly_seen)
    state["sites_started"] = sorted(started | newly_started)
    state["projects"] = projects
    save_state(state)


if __name__ == "__main__":
    if "--check" in sys.argv:
        check_sites()
    else:
        main()