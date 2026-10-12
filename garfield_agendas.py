"""
Step 3: Garfield Township agendas + handouts -> Claude lead cards -> email.
"""
import json
import os
import smtplib
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import fitz  # PyMuPDF
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
PDF_DIR = Path("pdfs")
MAX_NEW_PER_RUN = 3
MAX_CHARS = 200_000
HEADERS = {"User-Agent": "Mozilla/5.0 (lead-finder learning project)"}

PROMPT = """You are a lead analyst for a commercial HVAC contractor in Grand Traverse County, Michigan.
Below is a township planning commission agenda followed by its handouts (staff reports, site plans).
List every development project. Use the handouts to fill in details.
For each project give:
- Project: name
- Location: street address or area
- Type: (retail, apartments, restaurant, office, industrial, etc.)
- Size: square feet and/or number of units; "not stated" only if truly absent
- Developer/Applicant: name if stated
- Stage: (conceptual, site plan review, special use permit, approved, etc.)
- HVAC value: High / Medium / Low, with one short reason
Keep each project to these 7 lines. Skip routine items (minutes, reports, public comment).
If there are no projects, reply "No projects." Plain text only, no tables, no markdown.

DOCUMENTS:
"""


def load_seen():
    return set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2))


def find_meetings(url):
    soup = BeautifulSoup(requests.get(url, headers=HEADERS, timeout=30).text, "html.parser")
    meetings, seen_agendas = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        slug = href.lower().rstrip("/").split("/")[-1]
        if not slug.endswith(".pdf"):
            continue
        if slug.startswith("pc-agenda"):
            if href in seen_agendas:
                meetings.append(None)
                continue
            seen_agendas.add(href)
            meetings.append({"agenda": href, "title": a.get_text(" ", strip=True), "handouts": []})
        elif slug.startswith("pc-handouts") and meetings and meetings[-1]:
            meetings[-1]["handouts"].append(href)
    return [m for m in meetings if m]


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
    with fitz.open(path) as doc:
        text = "\n".join(page.get_text() for page in doc)
    cache.write_text(text, encoding="utf-8")
    return text


def ask_ai(text):
    r = requests.post(
        CLAUDE_URL,
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 8000,
              "messages": [{"role": "user", "content": PROMPT + text}]},
        timeout=240,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    data = r.json()
    out = "\n".join(b["text"] for b in data["content"] if b.get("type") == "text").strip()
    if data.get("stop_reason") == "max_tokens":
        out += "\n\n[Note: output was cut off - raise max_tokens]"
    return out


def send_email(sections):
    msg = EmailMessage()
    msg["Subject"] = f"HVAC Leads - Garfield Twp - {date.today():%b %d, %Y}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = EMAIL_TO
    body = "New planning commission leads (Garfield Township, Grand Traverse County)\n\n"
    body += ("\n\n" + "-" * 50 + "\n\n").join(sections)
    msg.set_content(body)

    try:  # port 465 (SSL)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            smtp.send_message(msg)
        return
    except Exception as e:
        print(f"  port 465 failed ({e}), trying 587...")

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:  # port 587 (STARTTLS)
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


def main():
    print("Lead finder v3.1 (email)")
    missing = [n for n, v in [("ANTHROPIC_API_KEY", API_KEY), ("GMAIL_ADDRESS", GMAIL_ADDRESS),
                              ("GMAIL_APP_PASSWORD", GMAIL_APP_PASSWORD)] if not v]
    if missing:
        print("Not set:", ", ".join(missing), "- run setx, then File > Exit and reopen VS Code.")
        return

    seen = load_seen()
    meetings = find_meetings(LISTING_URL)
    new = [m for m in meetings if m["agenda"] not in seen][:MAX_NEW_PER_RUN]
    print(f"Found {len(meetings)} meetings, {len(new)} new.\n")

    sections, done = [], []
    for m in new:
        print(f"Processing {m['title']} (+{len(m['handouts'])} handouts)...", flush=True)
        try:
            parts = ["=== AGENDA ===\n" + extract_text(download(m["agenda"]))]
            for h in m["handouts"]:
                parts.append("=== HANDOUT ===\n" + extract_text(download(h)))
            cards = ask_ai("\n\n".join(parts)[:MAX_CHARS])
        except Exception as e:
            print(f"  ! failed: {e}")
            continue
        sections.append(f"{m['title']}\nAgenda: {m['agenda']}\n\n{cards}")
        done.append(m["agenda"])
        print("  done.", flush=True)

    if not sections:
        print("No new leads. No email sent.")
        return

    try:
        send_email(sections)
        print(f"Email sent to {EMAIL_TO} with {len(sections)} meeting(s).")
    except Exception as e:
        print(f"! email failed: {e}")
        return

    seen.update(done)
    save_seen(seen)


if __name__ == "__main__":
    main()