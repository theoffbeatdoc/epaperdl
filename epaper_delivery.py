#!/usr/bin/env python3
"""
Daily ePaper Delivery Script
- Verifies date in paragraph before the Google Drive link
- Logs state to epaper_log.json (committed back to repo)
- Skips already-sent papers on retry runs
- Never posts error messages to Telegram
"""

import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup, NavigableString
from datetime import datetime
import pytz

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
LOG_FILE           = "epaper_log.json"
MAX_TG_FILE_SIZE   = 49 * 1024 * 1024   # 49 MB
TIMEOUT            = 60
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Date helpers ──────────────────────────────────────────────────────────────

def get_today_ist() -> datetime:
    return datetime.now(pytz.timezone("Asia/Kolkata"))

def date_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")

def date_display(dt: datetime) -> str:
    """e.g. '21 May, 2026'"""
    return dt.strftime("%-d %B, %Y")

def parse_date_from_text(text: str) -> datetime | None:
    """
    Parse a date string from paragraph text.
    Handles: '20 May 2026', '20 May, 2026', '20/05/2026', '20-05-2026'
    """
    # "20 May 2026" or "20 May, 2026"
    m = re.search(r'(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})', text)
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y"
            )
        except ValueError:
            pass
    # "20/05/2026" or "20-05-2026"
    m = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', text)
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)}/{m.group(2)}/{m.group(3)}", "%d/%m/%Y"
            )
        except ValueError:
            pass
    return None

def dates_match(parsed: datetime, today: datetime) -> bool:
    return (
        parsed.day == today.day
        and parsed.month == today.month
        and parsed.year == today.year
    )


# ── Log helpers ───────────────────────────────────────────────────────────────

def load_log() -> dict:
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as fh:
            return json.load(fh)
    return {}

def save_log(log: dict):
    with open(LOG_FILE, "w") as fh:
        json.dump(log, fh, indent=2, ensure_ascii=False)
    print(f"  [log] Saved {LOG_FILE}")

def set_log_entry(log: dict, today_key: str, name: str, status: str, detail: str = ""):
    log.setdefault(today_key, {})[name] = {
        "status":    status,       # "sent" | "pending"
        "detail":    detail,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ── Scraping ──────────────────────────────────────────────────────────────────

def _text_before_tag(tag) -> str:
    """Collect all text nodes that come before `tag` within its parent."""
    text = ""
    for node in tag.parent.contents:
        if node is tag:
            break
        if isinstance(node, NavigableString):
            text += str(node)
        elif hasattr(node, "get_text"):
            text += node.get_text()
    return text

def scrape_drive_link_verified(page_url: str, today: datetime) -> tuple[str | None, str]:
    """
    Fetch the page, find Google Drive <a> links, check the date in the
    surrounding paragraph text, and return (link, reason).
    reason is empty string on success, descriptive on failure.
    """
    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        return None, f"fetch_error: {exc}"

    soup = BeautifulSoup(resp.text, "lxml")

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if "drive.google.com" not in href:
            continue

        # ── Date check ──────────────────────────────────────────────────────
        # Try immediate parent first, then grandparent if empty
        text_before = _text_before_tag(a_tag).strip()
        if not text_before and a_tag.parent and a_tag.parent.parent:
            text_before = _text_before_tag(a_tag.parent).strip()

        print(f"  [D] Candidate link: {href[:70]}")
        print(f"  [D] Text before:    {text_before!r}")

        if not text_before:
            # Could be a sidebar / nav link with no date — skip
            print("  [D] No surrounding text, skipping candidate.")
            continue

        parsed_date = parse_date_from_text(text_before)
        if parsed_date is None:
            print("  [D] Could not parse a date from surrounding text, skipping.")
            continue

        if not dates_match(parsed_date, today):
            print(
                f"  [!] Date mismatch — found {parsed_date.strftime('%d %B %Y')}, "
                f"expected {today.strftime('%d %B %Y')}"
            )
            # Don't return yet; there may be a newer entry further down the page
            continue

        print(f"  [✓] Date confirmed: {parsed_date.strftime('%d %B %Y')}")
        return href, ""

    # Fell through — check if we found any drive links at all
    all_drive = [a["href"] for a in soup.find_all("a", href=True) if "drive.google.com" in a["href"]]
    if all_drive:
        return None, "date_mismatch_all_candidates"
    return None, "no_drive_link_found"


# ── Download ──────────────────────────────────────────────────────────────────

def extract_file_id(drive_url: str) -> str | None:
    for pat in [r"/file/d/([a-zA-Z0-9_-]{10,})",
                r"[?&]id=([a-zA-Z0-9_-]{10,})",
                r"/d/([a-zA-Z0-9_-]{10,})"]:
        m = re.search(pat, drive_url)
        if m:
            return m.group(1)
    return None

def download_pdf(drive_url: str, save_path: str) -> bool:
    file_id = extract_file_id(drive_url)
    if not file_id:
        return False

    session = requests.Session()
    session.headers.update(HEADERS)
    dl_url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"

    try:
        resp = session.get(dl_url, stream=True, timeout=TIMEOUT)
        resp.raise_for_status()

        # Handle large-file virus-scan page
        if "text/html" in resp.headers.get("Content-Type", ""):
            token_m = re.search(r'confirm=([0-9A-Za-z_-]+)', resp.text)
            if token_m:
                dl_url = (
                    f"https://drive.google.com/uc?export=download"
                    f"&id={file_id}&confirm={token_m.group(1)}"
                )
                resp = session.get(dl_url, stream=True, timeout=TIMEOUT)
                resp.raise_for_status()
            else:
                print("  [!] Got HTML page but no confirm token.")
                return False

        bytes_written = 0
        with open(save_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
                    bytes_written += len(chunk)

        if bytes_written < 1024:
            print(f"  [!] File too small ({bytes_written} B), likely an error page.")
            os.remove(save_path)
            return False

        print(f"  [✓] Downloaded {bytes_written / 1024 / 1024:.2f} MB")
        return True

    except Exception as exc:
        print(f"  [!] Download error: {exc}")
        if os.path.exists(save_path):
            os.remove(save_path)
        return False


# ── Telegram ──────────────────────────────────────────────────────────────────

def tg_send_message(text: str) -> dict:
    r = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        data={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     text,
            "parse_mode":               "HTML",
            "disable_web_page_preview": "false",
        },
        timeout=30,
    )
    return r.json()

def tg_send_document(filepath: str, caption: str) -> dict:
    with open(filepath, "rb") as fh:
        r = requests.post(
            f"{TELEGRAM_API}/sendDocument",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"document": fh},
            timeout=120,
        )
    return r.json()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today        = get_today_ist()
    today_str    = date_key(today)
    today_label  = date_display(today)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)                  # make sure paths resolve to repo root

    with open("epaperdl.json") as fh:
        papers: dict[str, str] = json.load(fh)

    log       = load_log()
    today_log = log.get(today_str, {})

    # Only process papers not yet successfully sent today
    pending = {
        name: url
        for name, url in papers.items()
        if today_log.get(name, {}).get("status") != "sent"
    }

    if not pending:
        print("✅ All papers already sent today — nothing to do.")
        return

    print(f"📋 Pending ({len(pending)}/{len(papers)}): {', '.join(pending)}")

    tmp_dir = "/tmp/epapers"
    os.makedirs(tmp_dir, exist_ok=True)
    sent_this_run = 0

    for name, page_url in pending.items():
        print(f"\n{'─'*60}")
        print(f"  📰  {name}")
        print(f"  🌐  {page_url}")

        # ── Scrape + date verify ─────────────────────────────────────────────
        drive_link, reason = scrape_drive_link_verified(page_url, today)

        if not drive_link:
            print(f"  [skip] {reason}")
            set_log_entry(log, today_str, name, "pending", reason)
            save_log(log)
            continue

        # ── Download PDF ─────────────────────────────────────────────────────
        safe_name = re.sub(r"[^\w\s-]", "", name).strip().replace(" ", "_")
        pdf_path  = f"{tmp_dir}/{safe_name}.pdf"
        caption   = f"📰 <b>{name}</b>\n📅 {today_label}"

        pdf_ok   = download_pdf(drive_link, pdf_path)
        pdf_size = os.path.getsize(pdf_path) if pdf_ok else 0

        # ── Send to Telegram ─────────────────────────────────────────────────
        sent = False
        method = ""

        if pdf_ok and pdf_size <= MAX_TG_FILE_SIZE:
            r = tg_send_document(pdf_path, caption)
            if r.get("ok"):
                sent, method = True, "pdf"
            else:
                print(f"  [!] sendDocument failed: {r.get('description')} — falling back to link")

        if not sent:
            # Send as link (either PDF failed, or file too large)
            size_note = (
                f"\n⚠️ PDF too large to attach ({pdf_size/1024/1024:.1f} MB)"
                if pdf_ok and pdf_size > MAX_TG_FILE_SIZE else ""
            )
            r = tg_send_message(f"{caption}{size_note}\n\n🔗 <a href='{drive_link}'>Download PDF</a>")
            if r.get("ok"):
                sent, method = True, "link"
            else:
                print(f"  [!] sendMessage also failed: {r.get('description')}")

        if sent:
            print(f"  [✓] Sent via {method}")
            set_log_entry(log, today_str, name, "sent", method)
            sent_this_run += 1
        else:
            set_log_entry(log, today_str, name, "pending", "telegram_send_failed")

        # Cleanup
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

        save_log(log)
        time.sleep(3)   # avoid Telegram flood limits

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    still_pending = [n for n, v in log.get(today_str, {}).items() if v["status"] != "sent"]
    print(f"  Sent this run : {sent_this_run}")
    print(f"  Still pending : {still_pending or 'none — all done!'}")


if __name__ == "__main__":
    main()