"""Remote job scraper with resume matching and international eligibility review.

Environment variables:
  Required: RAPIDAPI_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  Optional: GSHEET_CREDENTIALS, GSHEET_ID, TEST_MODE=true

Set TEST_MODE=true to search, score, and print results without sending Telegram
messages or writing to Google Sheets/seen_jobs.txt.
"""

import html
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    import gspread
    from google.oauth2.service_account import Credentials
    SHEETS_AVAILABLE = True
except ImportError:
    SHEETS_AVAILABLE = False


# ============================ EDIT THIS SECTION =============================

SEARCH_QUERIES = [
    "full stack developer Python React remote",
    "Node.js developer remote worldwide",
]

# Skill weights: increase the weight of skills that matter most in your resume.
TARGET_SKILLS = {
    "node.js": 4,
    "javascript": 3,
    "react": 3,
    "postgresql": 3,
    "rest api": 3,
    "docker": 2,
    "aws": 1,
}

PREFERRED_TERMS = [
    "worldwide", "international", "global", "work from anywhere",
    "remote anywhere", "independent contractor", "contractor",
]

# These normally indicate the job is not suitable for you.
HARD_RESTRICTIONS = [
    "us residents only", "us citizens only",
    "must reside in the us", "must be located in the us",
    "must be based in the us", "authorized to work in the united states",
    "security clearance", "government clearance", "canada only",
    "uk only", "europe only",
]

# These are flagged for manual review rather than automatically rejected.
ELIGIBILITY_REVIEW_TERMS = [
    "iran", "sanctions", "ofac", "embargo", "restricted countries",
    "countries we cannot hire from", "payment restrictions", "us person",
]

MIN_MATCH_SCORE = 3
MAX_JOBS_PER_RUN = 15
MAX_SEEN_JOBS = 2000
SEEN_JOBS_FILE = Path("seen_jobs.txt")
GSHEET_SHEET_NAME = "Jobs"

# ============================================================================

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GSHEET_CREDENTIALS = os.environ.get("GSHEET_CREDENTIALS", "")
GSHEET_ID = os.environ.get("GSHEET_ID", "")
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() in {"1", "true", "yes"}


def load_seen_jobs():
    if not SEEN_JOBS_FILE.exists():
        return set()
    return {line.strip() for line in SEEN_JOBS_FILE.read_text().splitlines()
            if line.strip()}


def save_seen_jobs(seen):
    # Sorting makes the file deterministic; the newest IDs are not guaranteed
    # to be last, but the size remains bounded.
    ids = sorted(seen)[-MAX_SEEN_JOBS:]
    SEEN_JOBS_FILE.write_text("\n".join(ids) + ("\n" if ids else ""))


def search_jobs(query, retries=3):
    url = "https://jsearch.p.rapidapi.com/search"
    headers = {"x-rapidapi-key": RAPIDAPI_KEY,
               "x-rapidapi-host": "jsearch.p.rapidapi.com"}
    params = {"query": query, "num_pages": "1", "date_posted": "3days",
              "work_from_home": "true"}

    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
            if response.status_code == 429:
                wait = min(60 * attempt, 180)
                log.warning("Rate limited; waiting %ss", wait)
                time.sleep(wait)
                continue
            if response.status_code == 403:
                log.error("RapidAPI key is invalid or not subscribed")
                return []
            response.raise_for_status()
            data = response.json()
            return data.get("data", []) if data.get("status") == "OK" else []
        except (requests.RequestException, ValueError) as exc:
            log.warning("Search attempt %s/%s failed: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(5 * attempt)
    return []


def job_id(job):
    return job.get("job_id") or job.get("job_apply_link") or job.get("job_google_link")


def job_text(job):
    return " ".join(str(job.get(field) or "") for field in (
        "job_title", "job_description", "job_employment_type",
        "job_required_skills", "job_city", "job_country")).lower()


def has_any(text, terms):
    return [term for term in terms if term.lower() in text]


def analyze_job(job):
    text = job_text(job)
    matched_skills = [skill for skill in TARGET_SKILLS if skill.lower() in text]
    preferred = has_any(text, PREFERRED_TERMS)
    restrictions = has_any(text, HARD_RESTRICTIONS)
    review_terms = has_any(text, ELIGIBILITY_REVIEW_TERMS)
    score = sum(TARGET_SKILLS[skill] for skill in matched_skills)
    score += 3 * len(preferred)
    score -= 20 * len(restrictions)
    return score, matched_skills, preferred, restrictions, review_terms


def extract_salary(job):
    if job.get("job_salary_string"):
        return str(job["job_salary_string"])
    minimum, maximum = job.get("job_min_salary"), job.get("job_max_salary")
    period = (job.get("job_salary_period") or "").lower()
    suffix = {"year": "/yr", "month": "/mo", "hour": "/hr", "week": "/wk"}.get(period, "")
    try:
        if minimum and maximum:
            return f"${int(minimum):,} – ${int(maximum):,}{suffix}"
        if minimum:
            return f"${int(minimum):,}+{suffix}"
    except (TypeError, ValueError):
        pass
    return ""


def format_job(job):
    score, skills, preferred, restrictions, review_terms = analyze_job(job)
    title = html.escape(job.get("job_title") or "Untitled")
    company = html.escape(job.get("employer_name") or "Unknown")
    location = html.escape(
        ", ".join(x for x in (job.get("job_city"), job.get("job_country")) if x)
        or "Remote")
    link = job.get("job_apply_link") or job.get("job_google_link") or ""
    link = html.escape(str(link), quote=True)
    lines = [f"💼 <b>{title}</b>", f"🏢 {company}", f"📍 {location}",
             f"🎯 Match: <b>{score}</b> ({', '.join(skills) or 'none'})"]
    if extract_salary(job):
        lines.append(f"💰 <b>{html.escape(extract_salary(job))}</b>")
    if preferred:
        lines.append("🌍 International-friendly terms found")
    if restrictions:
        lines.append("⛔ Location restriction found")
    if review_terms:
        lines.append("⚠️ Review Iran/payment eligibility manually")
    if link:
        lines.append(f'<a href="{link}">Apply Now</a>')
    return "\n".join(lines)


def send_telegram(text):
    if TEST_MODE:
        print("\n" + "-" * 70 + "\n" + text)
        return True
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram environment variables are missing")
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=15)
    response.raise_for_status()
    return True


def append_to_sheet(job):
    if TEST_MODE or not SHEETS_AVAILABLE or not GSHEET_CREDENTIALS or not GSHEET_ID:
        return
    try:
        credentials = Credentials.from_service_account_info(
            json.loads(GSHEET_CREDENTIALS),
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"])
        sheet = gspread.authorize(credentials).open_by_key(GSHEET_ID).worksheet(GSHEET_SHEET_NAME)
        sheet.append_row([
            job.get("job_title", ""), job.get("employer_name", ""),
            job.get("job_apply_link") or job.get("job_google_link") or "",
            (job.get("job_posted_at_datetime_utc") or "")[:10],
            job.get("job_city", ""), job.get("job_country", ""),
            extract_salary(job), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        ], value_input_option="USER_ENTERED")
    except Exception as exc:
        log.error("Google Sheets append failed: %s", exc)


def main():
    if not RAPIDAPI_KEY:
        raise RuntimeError("RAPIDAPI_KEY is missing")
    seen = set() if TEST_MODE else load_seen_jobs()
    candidates, found_ids = {}, set()

    for query in SEARCH_QUERIES:
        log.info("Searching: %s", query)
        for job in search_jobs(query):
            jid = job_id(job)
            if not jid or jid in seen or jid in found_ids:
                continue
            found_ids.add(jid)
            score, _, _, restrictions, _ = analyze_job(job)
            if not restrictions and score >= MIN_MATCH_SCORE:
                candidates[jid] = job
        time.sleep(1.5)

    ranked = sorted(candidates.values(),
                    key=lambda item: analyze_job(item)[0], reverse=True)
    log.info("Found %s matching jobs", len(ranked))

    if not ranked:
        send_telegram("🔍 <b>Daily report</b>\nNo matching jobs found.")
    else:
        for job in ranked[:MAX_JOBS_PER_RUN]:
            send_telegram(format_job(job))
            if not TEST_MODE:
                seen.add(job_id(job))  # Mark seen only after successful delivery.
                append_to_sheet(job)
            time.sleep(0.8)

    if not TEST_MODE:
        save_seen_jobs(seen)


if __name__ == "__main__":
    main()
