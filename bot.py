import requests
import os
import html
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

# ─── Optional: Google Sheets ──────────────────────────────────────────────────
try:
    import gspread
    from google.oauth2.service_account import Credentials
    SHEETS_AVAILABLE = True
except ImportError:
    SHEETS_AVAILABLE = False

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
RAPIDAPI_KEY       = os.environ["RAPIDAPI_KEY"]
TELEGRAM_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
GSHEET_CREDENTIALS = os.environ.get("GSHEET_CREDENTIALS", "")   
GSHEET_ID          = os.environ.get("GSHEET_ID", "")
GSHEET_SHEET_NAME  = "Jobs"

SEEN_JOBS_FILE    = Path("seen_jobs.txt")
MAX_SEEN_JOBS     = 2000   
MAX_JOBS_PER_RUN  = 15     

# ─── Search Queries ───────────────────────────────────────────────────────────
# Removed "worldwide" to broaden the net, relying on Python to filter the bad ones out.
SEARCH_QUERIES = [
   "fullstack developer",
"software engineer","ai product developer"
]

# ─── Strict Filters ───────────────────────────────────────────────────────────
EXCLUDED_COUNTRIES = [
    "us", "usa", "united states", "united states of america", "uk", "united kingdom"
]

BLACKLIST_KEYWORDS = [
    # US / Regional Restrictions
    "us residents only", "must reside in us", "must be located in the us", 
    "must be based in", "us only", "uk only", "eu only", "europe only",
    "must be a us citizen", "us citizenship", "green card", "clearance",
    "eligible to work in", "work authorization", "right to work",
    "north america only", "latin america only",
    
    # Timezone restrictions often implying local remote
    "est", "pst", "cst", "pacific time", "eastern time", "overlap with us",
    
    # Excluded Roles
    "director"
]

# ══════════════════════════════════════════════════════════════════════════════
# Cache — seen_jobs.txt
# ══════════════════════════════════════════════════════════════════════════════

def load_seen_jobs() -> set:
    if SEEN_JOBS_FILE.exists():
        ids = set(line.strip() for line in SEEN_JOBS_FILE.read_text().splitlines() if line.strip())
        log.info(f"Loaded {len(ids)} seen job IDs from cache")
        return ids
    log.info("No cache file found — starting fresh")
    return set()

def save_seen_jobs(seen: set) -> None:
    ids_list = list(seen)
    if len(ids_list) > MAX_SEEN_JOBS:
        ids_list = ids_list[-MAX_SEEN_JOBS:]
    SEEN_JOBS_FILE.write_text("\n".join(ids_list))
    log.info(f"Saved {len(ids_list)} job IDs to cache")

# ══════════════════════════════════════════════════════════════════════════════
# JSearch API
# ══════════════════════════════════════════════════════════════════════════════

def search_jobs(query: str, retries: int = 3) -> list:
    url = "https://jsearch.p.rapidapi.com/search"
    headers = {
        "x-rapidapi-key":  RAPIDAPI_KEY,
        "x-rapidapi-host": "jsearch.p.rapidapi.com",
    }
    params = {
        "query":          query,
        "num_pages":      "2", # Increased to fetch more base results since we filter heavily
        "date_posted":    "3days",
        "work_from_home": "true",
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=20)
            if resp.status_code == 429:
                time.sleep(60)
                continue
            if resp.status_code == 403:
                return []
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "OK":
                return []
            return data.get("data", [])
        except Exception as e:
            log.error(f"Request error: {e}")
        if attempt < retries:
            time.sleep(5 * attempt)
    return []

# ══════════════════════════════════════════════════════════════════════════════
# Filters
# ══════════════════════════════════════════════════════════════════════════════

def is_blacklisted(job: dict) -> bool:
    # 1. Filter by Country strictly
    country = (job.get("job_country") or "").lower().strip()
    if country in EXCLUDED_COUNTRIES:
        log.info(f"  ⛔ Blacklisted '{job.get('job_title')}' — Country restriction: {country}")
        return True

    # 2. Filter by Keywords in Title and Description
    description = (job.get("job_description") or "").lower()
    title       = (job.get("job_title") or "").lower()
    combined    = f"{title} \n {description}"

    for keyword in BLACKLIST_KEYWORDS:
        # Pad keyword with spaces to avoid partial word matching (e.g. 'est' matching 'test')
        if f" {keyword} " in f" {combined} " or f" {keyword}," in combined or f" {keyword}." in combined:
            log.info(f"  ⛔ Blacklisted '{job.get('job_title')}' — matched keyword: '{keyword}'")
            return True
            
    return False

# ══════════════════════════════════════════════════════════════════════════════
# Telegram & Formatting
# ══════════════════════════════════════════════════════════════════════════════

def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        return resp.ok
    except Exception:
        return False

def extract_salary(job: dict) -> str:
    if job.get("job_salary_string"):
        return job["job_salary_string"]
    min_s  = job.get("job_min_salary")
    max_s  = job.get("job_max_salary")
    period = (job.get("job_salary_period") or "").lower()
    period_map = {"year": "/yr", "month": "/mo", "hour": "/hr", "week": "/wk"}
    period_label = period_map.get(period, f"/{period}" if period else "")

    if min_s and max_s: return f"${int(min_s):,} – ${int(max_s):,}{period_label}"
    if min_s:           return f"${int(min_s):,}+{period_label}"
    return ""

def format_job(job: dict) -> str:
    title    = html.escape(job.get("job_title")    or "بدون عنوان")
    company  = html.escape(job.get("employer_name") or "نامشخص")
    city     = html.escape(job.get("job_city")     or "")
    country  = html.escape(job.get("job_country")  or "")
    location = f"{city}, {country}".strip(", ") or "Worldwide Remote"
    source   = html.escape(job.get("job_publisher") or "")
    link     = job.get("job_apply_link") or job.get("job_google_link") or ""
    salary   = extract_salary(job)

    lines = [
        f"💼 <b>{title}</b>",
        f"🏢 {company}",
        f"📍 {location}",
    ]
    if salary: lines.append(f"💰 <b>{html.escape(salary)}</b>")
    if source: lines.append(f"🌐 {source}")
    if link:   lines.append(f'🔗 <a href="{link}">Apply Now</a>')

    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# Main Logic
# ══════════════════════════════════════════════════════════════════════════════

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log.info(f"═══ Bot started at {now} ═══")

    seen_jobs     = load_seen_jobs()
    
    new_jobs      = []
    blacklisted   = 0
    already_seen  = 0
    errors        = 0

    for query in SEARCH_QUERIES:
        jobs = search_jobs(query)
        for job in jobs:
            try:
                job_id = job.get("job_id") or job.get("job_apply_link") or ""
                if not job_id: continue

                if job_id in seen_jobs:
                    already_seen += 1
                    continue

                seen_jobs.add(job_id)

                if is_blacklisted(job):
                    blacklisted += 1
                    continue

                new_jobs.append(job)
            except Exception:
                errors += 1
                continue
        time.sleep(1.5)

    dedup_seen = set()
    unique_jobs = []
    for job in new_jobs:
        jid = job.get("job_id", "")
        if jid and jid not in dedup_seen:
            dedup_seen.add(jid)
            unique_jobs.append(job)

    log.info(f"Summary → new: {len(unique_jobs)} | blacklisted: {blacklisted} | already seen: {already_seen} | errors: {errors}")

    if not unique_jobs:
        send_telegram(
            f"🔍 <b>گزارش روزانه</b>\n📅 {now}\n\n✅ آگهی جدیدی امروز پیدا نشد.\n⛔ فیلتر شده: {blacklisted} | 🔁 تکراری: {already_seen}"
        )
        save_seen_jobs(seen_jobs)
        return

    send_telegram(
        f"🔍 <b>آگهی‌های شغلی جدید (بین‌المللی)</b>\n📅 {now}\n📊 {len(unique_jobs)} آگهی جدید | ⛔ {blacklisted} فیلتر شد\n➖➖➖➖➖➖➖➖"
    )
    time.sleep(1)

    sent = 0
    for job in unique_jobs[:MAX_JOBS_PER_RUN]:
        try:
            msg = format_job(job)
            if send_telegram(msg):
                sent += 1
            time.sleep(0.8)
        except Exception:
            continue

    save_seen_jobs(seen_jobs)
    log.info(f"═══ Done. Sent {sent}/{len(unique_jobs)} jobs ═══")

if __name__ == "__main__":
    main()
