#!/usr/bin/env python3
"""
Daily ePaper Delivery Script
Scrapes Google Drive links from ePaper sites, downloads PDFs,
and delivers them to a Telegram group.
"""

import json
import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import pytz

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_TG_FILE_SIZE = 49 * 1024 * 1024  # 49 MB (Telegram bot limit is 50 MB)
TIMEOUT          = 60                 # seconds


# ── Helpers ───────────────────────────────────────────────────────────────────

def date_string() -> str:
    """Returns today's date in 'D Month, YYYY' format (IST)."""
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    return now.strftime("%-d %B, %Y")


def scrape_drive_link(page_url: str) -> str | None:
    """Fetch the page and return the first drive.google.com URL found."""
    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        print(f"  [!] Failed to fetch page {page_url}: {exc}")
        return None

    soup = BeautifulSoup(resp.text, "lxml")

    # 1. <a href="...drive.google.com...">
    for tag in soup.find_all("a", href=True):
        if "drive.google.com" in tag["href"]:
            return tag["href"].strip()

    # 2. <iframe src="...drive.google.com...">
    for tag in soup.find_all("iframe", src=True):
        if "drive.google.com" in tag["src"]:
            return tag["src"].strip()

    # 3. Regex over raw HTML (catches JS-embedded links)
    matches = re.findall(
        r'https://drive\.google\.com/(?:file/d/|open\?id=|uc\?)[^\s"\'<>\\]+',
        resp.text,
    )
    if matches:
        # Clean trailing punctuation / escape chars
        return re.split(r'[\\"\'\s>]', matches[0])[0]

    return None


def extract_file_id(drive_url: str) -> str | None:
    """Extract the file ID from any Google Drive URL variant."""
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]{10,})",   # /file/d/FILE_ID/...
        r"[?&]id=([a-zA-Z0-9_-]{10,})",     # ?id=FILE_ID or &id=FILE_ID
        r"/d/([a-zA-Z0-9_-]{10,})",          # /d/FILE_ID
    ]
    for pat in patterns:
        m = re.search(pat, drive_url)
        if m:
            return m.group(1)
    return None


def clean_drive_url(drive_url: str) -> str:
    """
    Normalise to a direct-download URL so we don't land on the preview page.
    Handles both /file/d/ID/view and /open?id=ID style links.
    """
    file_id = extract_file_id(drive_url)
    if file_id:
        return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
    return drive_url  # fall back to original


def download_pdf(drive_url: str, save_path: str) -> bool:
    """
    Download a PDF from Google Drive to save_path.
    Handles the 'large file' virus-scan warning page automatically.
    Returns True on success.
    """
    dl_url = clean_drive_url(drive_url)
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        resp = session.get(dl_url, stream=True, timeout=TIMEOUT)
        resp.raise_for_status()

        # Google Drive may redirect to a warning page for large files
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" in content_type:
            # Parse the confirmation token
            html = resp.text
            token_match = re.search(r'confirm=([0-9A-Za-z_-]+)', html)
            if token_match:
                token = token_match.group(1)
                file_id = extract_file_id(drive_url)
                dl_url = (
                    f"https://drive.google.com/uc?export=download"
                    f"&id={file_id}&confirm={token}"
                )
                resp = session.get(dl_url, stream=True, timeout=TIMEOUT)
                resp.raise_for_status()
            else:
                print("  [!] Hit HTML page but found no confirm token — skipping download.")
                return False

        # Stream to disk
        bytes_written = 0
        with open(save_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
                    bytes_written += len(chunk)

        if bytes_written < 1024:
            print(f"  [!] Downloaded file is suspiciously small ({bytes_written} B) — treating as failure.")
            os.remove(save_path)
            return False

        print(f"  [✓] Downloaded {bytes_written / 1024 / 1024:.2f} MB → {save_path}")
        return True

    except Exception as exc:
        print(f"  [!] Download error: {exc}")
        if os.path.exists(save_path):
            os.remove(save_path)
        return False


# ── Telegram ──────────────────────────────────────────────────────────────────

def tg_send_message(text: str) -> dict:
    resp = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        },
        timeout=30,
    )
    return resp.json()


def tg_send_document(filepath: str, caption: str) -> dict:
    with open(filepath, "rb") as fh:
        resp = requests.post(
            f"{TELEGRAM_API}/sendDocument",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption,
                "parse_mode": "HTML",
            },
            files={"document": fh},
            timeout=120,
        )
    return resp.json()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    json_path  = os.path.join(script_dir, "epaperdl.json")

    with open(json_path) as fh:
        papers: dict[str, str] = json.load(fh)

    date_str  = date_string()
    tmp_dir   = "/tmp/epapers"
    os.makedirs(tmp_dir, exist_ok=True)

    errors = []

    for name, page_url in papers.items():
        print(f"\n{'─'*60}")
        print(f"  📰  {name}")
        print(f"  🔗  {page_url}")

        caption = f"📰 <b>{name}</b>\n📅 {date_str}"

        # Step 1: Scrape
        drive_link = scrape_drive_link(page_url)
        if not drive_link:
            msg = f"📰 <b>{name}</b> — {date_str}\n⚠️ Could not find a Google Drive link on the source page."
            print("  [!] No Drive link found — sending warning message.")
            tg_send_message(msg)
            errors.append(name)
            time.sleep(2)
            continue

        print(f"  [✓] Drive link: {drive_link}")

        # Step 2: Download PDF
        safe_name = re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')
        pdf_path  = os.path.join(tmp_dir, f"{safe_name}_{date_str.replace(', ', '_').replace(' ', '_')}.pdf")

        pdf_ok = download_pdf(drive_link, pdf_path)

        # Step 3: Send to Telegram
        if pdf_ok:
            file_size = os.path.getsize(pdf_path)
            if file_size <= MAX_TG_FILE_SIZE:
                print("  [→] Sending PDF to Telegram …")
                result = tg_send_document(pdf_path, caption)
                if result.get("ok"):
                    print("  [✓] Sent PDF successfully.")
                else:
                    print(f"  [!] Telegram error: {result}")
                    # Fall back to link
                    tg_send_message(f"{caption}\n\n🔗 <a href='{drive_link}'>Open / Download</a>")
            else:
                print(f"  [!] PDF too large ({file_size/1024/1024:.1f} MB) for Telegram — sending link.")
                tg_send_message(f"{caption}\n\n🔗 <a href='{drive_link}'>Open / Download</a>\n⚠️ File too large to attach ({file_size/1024/1024:.1f} MB)")
            # Cleanup
            try:
                os.remove(pdf_path)
            except OSError:
                pass
        else:
            print("  [→] Download failed — sending link to Telegram …")
            tg_send_message(f"{caption}\n\n🔗 <a href='{drive_link}'>Open / Download</a>")

        time.sleep(3)  # avoid Telegram rate-limiting

    print(f"\n{'═'*60}")
    if errors:
        print(f"  Done with errors for: {', '.join(errors)}")
    else:
        print("  All papers processed successfully.")


if __name__ == "__main__":
    main()