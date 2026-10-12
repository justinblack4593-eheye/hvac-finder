"""
Garfield Township agendas + handouts -> Claude lead cards -> email.
v3.5: remembers projects; older repeats are skipped, newer ones show as UPDATE.
"""
import json
import os
import re
import smtplib
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import pymupdf
import requests
from bs4 import BeautifulSoup

LISTING_URL = "https://www.garfieldmi.gov/boards-and-commissions/planning-commission/"

API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
MODEL = "claude-haiku-5-5"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"

GMAIL_ADDRESS = (os.environ.get("GMAIL_ADDRESS") or "").strip()
GMAIL_APP_PASSWORD = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
EMAIL_TO = (os.environ.get("EMAIL_TO") or GMAIL_ADDRESS).strip()

SEEN_FILE = Path("seen_garfield.json")
PROJECTS_FILE = Path("projects_garfield.json")
PDF_DIR = Path("pdfs")
MAX_NEW_PER_RUN = 3
MAX_CHARS = 200_000
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PROMPT = """You are a lead analyst for a commercial HVAC contractor in Grand Traverse County, Michigan.
Below are a township planning commission agenda and its handouts (staff reports, site plans).
Each document starts with a marker like [D1], [D2].
List every development project. Use the handouts to fill in details.
For each project give exactly these lines, with a blank line between projects:
- Key: (see KNOWN PROJECTS rules below)
- Project: name
- Case: PD number if stated, else "none"
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
Skip routine items (minutes, reports, public comment).
If there are no projects, reply "No projects." Plain text only, no tables, no markdown.

KNOWN PROJECTS from earlier meetings. If a project in these documents is the same development
as one listed (same site or address, even if renamed, amended, or at a different stage),
use its exact Key. Otherwise create a new Key: lowercase words joined by hyphens,
like 4040-creekside-apartments.
{known}

DOCUMENTS:
"""


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def find_meetings(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")
    meetings, seen_agendas = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        slug = href.lower().rstrip("/").split("/")[-1]
        if not slug.endswith(".pdf"):
            continue
        label = a.get_text(" ", strip=True)
        if slug.startswith("pc-agenda"):
            if href in seen_agendas:
                meetings.append(None)
                continue
            seen_agendas.add(href)
            meetings.append({"agenda": href, "title": label, "handouts": []})
        elif slug.startswith("pc-handouts") and meetings and meetings[-1]:
            meetings[-1]["handouts"].append((href, label))
    meetings = [m for m in meetings if m]
    if not meetings:
        title = soup.title.get_text(strip=True) if soup.title else "(no title)"
        print(f"DEBUG: status {resp.status_code}, page title: {title}")
    return meetings


def agenda_order(url):
    """Garfield's download IDs go up over time, so a bigger ID = a newer meeting."""
    try:
        return int(url.rstrip("/").split("/")[-2])
    except ValueError:
        return 0


def download(url):
    PDF_DIR.mkdir(exist_ok=True)
    parts = url.rstrip("/").split("/")
    path = PDF_DIR / f"{parts[-2]}_{parts[-1]}"
    if not path.exists():
        path.write_bytes(requests.get(url, headers=HEADERS, timeout=120).content)
    return path


def extract_text(path):
    cache = path.with_suffix(".txt")
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    with pymupdf.open(path) as doc:
        text = "\n".join(page.get_text() for page in doc)
    cache.write_text(text, encoding="utf-8")
    return text


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
    out = "\n".join(b["text"] for b in data["content"] if b.get("type") == "text").strip()
    if data.get("stop_reason") == "max_tokens":
        print("  ! warning: output was cut off - some projects may be missing")
    return out


def clean(line):
    return re.sub(r"^[\-\*\d\.\)\s]+", "", line.strip())


def field(block, name):
    for line in block.splitlines():
        s = clean(line)
        if s.lower().startswith(name.lower() + ":"):
            return s.split(":", 1)[1].strip()
    return ""


def filter_cards(cards, meeting_title, order, projects):
    """Keep only projects that are new, or newer than what we've already seen."""
    kept = []
    for block in re.split(r"\n\s*\n", cards):
        if not field(block, "Project"):
            continue
        key = field(block, "Key").lower().strip()
        if not key:
            key = re.sub(r"[^a-z0-9]+", "-", field(block, "Project").lower()).strip("-")
        prev = projects.get(key)
        if prev and prev["order"] >= order:
            continue  # same or older news than what we already have
        tag = f"UPDATE - previously: {prev['stage']} ({prev['meeting']})" if prev else "NEW"
        body = "\n".join(l for l in block.strip().splitlines()
                         if not clean(l).lower().startswith("key:"))
        kept.append(f"[{tag}]\n{body}")
        projects[key] = {"name": field(block, "Project"), "stage": field(block, "Stage"),
                         "meeting": meeting_title, "order": order}
    return kept


def fill_links(text, docs):
    def repl(match):
        n = int(match.group(1))
        if 1 <= n <= len(docs):
            label, href = docs[n - 1]
            return f"\n    {label}: {href}"
        return ""
    return re.sub(r"\[D(\d+)\]", repl, text)


def send_email(sections):
    msg = EmailMessage()
    msg["Subject"] = f"HVAC Leads - Garfield Twp - {date.today():%b %d, %Y}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = EMAIL_TO
    body = "New planning commission leads (Garfield Township, Grand Traverse County)\n\n"
    body += ("\n\n" + "=" * 50 + "\n\n").join(sections)
    msg.set_content(body)

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


def main():
    print("Lead finder v3.5 (project tracking)")
    missing = [n for n, v in [("ANTHROPIC_API_KEY", API_KEY), ("GMAIL_ADDRESS", GMAIL_ADDRESS),
                              ("GMAIL_APP_PASSWORD", GMAIL_APP_PASSWORD)] if not v]
    if missing:
        print("Not set:", ", ".join(missing), "- run setx, then File > Exit and reopen VS Code.")
        return

    seen = set(load_json(SEEN_FILE, []))
    projects = load_json(PROJECTS_FILE, {})
    meetings = find_meetings(LISTING_URL)
    new = [m for m in meetings if m["agenda"] not in seen][:MAX_NEW_PER_RUN]
    print(f"Found {len(meetings)} meetings, {len(new)} new. Tracking {len(projects)} projects.\n")

    sections, done = [], []
    for m in new:
        print(f"Processing {m['title']} (+{len(m['handouts'])} handouts)...", flush=True)
        docs = [(m["title"], m["agenda"])] + [(label, href) for href, label in m["handouts"]]
        known = "\n".join(f"{k} | {v['name']} | last stage: {v['stage']}"
                          for k, v in projects.items()) or "(none yet)"
        try:
            parts = [f"[D{i}] {label}\n" + extract_text(download(href))
                     for i, (label, href) in enumerate(docs, start=1)]
            cards = ask_ai(PROMPT.replace("{known}", known) + "\n\n".join(parts)[:MAX_CHARS])
        except Exception as e:
            print(f"  ! failed: {e}")
            continue

        done.append(m["agenda"])
        kept = filter_cards(cards, m["title"], agenda_order(m["agenda"]), projects)
        if not kept:
            print("  nothing new (only repeats of newer info).", flush=True)
            continue
        all_docs = "\n".join(f"  {label}: {href}" for label, href in docs)
        sections.append(f"{m['title']}\n\n" + fill_links("\n\n".join(kept), docs)
                        + f"\n\nAll documents for this meeting:\n{all_docs}")
        print(f"  {len(kept)} project(s) to report.", flush=True)

    if sections:
        try:
            send_email(sections)
            print(f"Email sent to {EMAIL_TO} with {len(sections)} meeting(s).")
        except Exception as e:
            print(f"! email failed: {e}")
            return
    else:
        print("No new leads. No email sent.")

    seen.update(done)
    save_json(SEEN_FILE, sorted(seen))
    save_json(PROJECTS_FILE, projects)


if __name__ == "__main__":
    main()