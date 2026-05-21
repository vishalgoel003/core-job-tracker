"""
workday_scraper.py — Task 2: Workday API Scraper Engine
---------------------------------------------------------
Fetches active job listings from Workday's undocumented but stable CXS API.

Endpoint pattern (derived from ./reference/ats-scrapers reference):
    POST https://{company}.{instance}.myworkdayjobs.com/wday/cxs/{company}/{site}/jobs

Key design decisions applied from user course-corrections:
  1. fetch_jobs() returns normalized flat dicts aligned with master_jobs.csv columns,
     NOT raw Workday JSON.
  2. A realistic browser User-Agent header is sent on every request to avoid
     corporate firewall 403s.
  3. Pagination is bounded by the `total` integer field in the Workday response
     root — NOT by list-length differentials.
  4. Module is fully importable: no side effects at import time. All logic lives
     in named, callable functions.

Usage (inside activated venv):
    python workday_scraper.py

Import usage (future orchestration runner):
    from workday_scraper import fetch_jobs
    from config_engine import load_config, resolve_output_paths

AGENT.md compliance:
  - Pure requests library. No Playwright, Puppeteer, or browser automation.
  - On any non-200 response: immediately halt, print raw status + headers, stop.
  - No silent error swallowing.
"""

import datetime
import sys
import time
from typing import Any

import requests

import config_engine

# ---------------------------------------------------------------------------
# Constants — derived from reference/ats-scrapers/src/jobhive/scrapers/workday.py
# ---------------------------------------------------------------------------

# Workday hard-caps `limit` at 20. Sending >20 returns HTTP 400.
PAGE_LIMIT: int = 20

# Polite inter-request delay between paginated POSTs.
# Barclays' Workday edge uses a sliding-window rate limiter that cuts
# rapid bursts. 3 seconds between pages keeps us well under its threshold
# while still completing a 230-job scrape in ~36 seconds.
INTER_REQUEST_DELAY: float = 3.0

# A realistic browser User-Agent avoids instant 403s from corporate firewalls
# (Barclays, HSBC, etc. filter bot-like UA strings at the edge).
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Required headers for Workday's CXS JSON API
_BASE_HEADERS: dict[str, str] = {
    "Content-Type":  "application/json",
    "Accept":        "application/json",
    "User-Agent":    USER_AGENT,
}


# ---------------------------------------------------------------------------
# Schema normalization — Course-correction #1
# ---------------------------------------------------------------------------

def _normalize_job(raw_item: dict[str, Any], company_name: str, base_url: str) -> dict[str, str]:
    """
    Map a single raw Workday job posting dict into a flat normalized dict whose
    keys align with the master_jobs.csv column schema:

        job_id, title, first_discovered_on, last_date, visible, relevance, applied

    Additional context fields (url, location, employment_type) are appended
    because the state-tracking module (Task 3) needs them for enriched Markdown
    output — they do NOT break the CSV header contract.

    Reference for field extraction:
        ./reference/ats-scrapers/src/jobhive/scrapers/workday.py  _parse_job()
    """
    external_path: str = raw_item.get("externalPath") or ""

    # job_id: prefer bulletFields[0] (requisition ID); fall back to last path segment
    bullet_fields: list = raw_item.get("bulletFields") or []
    bullet_id: str | None = bullet_fields[0] if bullet_fields else None
    job_id: str = (
        bullet_id
        or (external_path.rsplit("/", 1)[-1] if external_path else "")
        or "unknown"
    )

    title: str = (raw_item.get("title") or "Untitled").strip()

    # Public posting URL
    url: str = f"{base_url}{external_path}" if external_path else base_url

    # Location — Workday returns a pre-joined string via `locationsText`
    location: str = (raw_item.get("locationsText") or "").strip()

    # Employment type from `timeType` (e.g. "Full time", "Part time")
    employment_type: str = (raw_item.get("timeType") or "").strip()

    today: str = datetime.date.today().isoformat()

    return {
        # --- master_jobs.csv required columns ---
        "job_id":              job_id,
        "title":               title,
        "first_discovered_on": today,   # Task 3 will only write this on first insert
        "last_date":           today,
        "visible":             "yes",   # Active listing — visible by definition
        "relevance":           "TBD",   # User-assigned; default from global_settings
        "applied":             "no",    # Default: not yet applied

        # --- enrichment fields for Markdown detail files (Task 3) ---
        "company":             company_name,
        "url":                 url,
        "location":            location,
        "employment_type":     employment_type,
    }


# ---------------------------------------------------------------------------
# Core API call — single page
# ---------------------------------------------------------------------------

def _post_page(
    api_url:        str,
    payload:        dict[str, Any],
    offset:         int,
) -> dict[str, Any]:
    """
    POST one paginated request to the Workday CXS jobs endpoint.

    AGENT.md Rule 5 — error handling:
      - Non-200 response: immediately halt. Print raw HTTP status code and
        all response headers. Raise SystemExit so the caller surfaces cleanly
        without attempting any automatic fix.
      - JSON parse failure: halt and dump raw response body.

    Returns the parsed response dict on HTTP 200.
    """
    page_payload = {**payload, "limit": PAGE_LIMIT, "offset": offset}

    print(f"    → POST {api_url}  offset={offset}")

    try:
        response = requests.post(
            api_url,
            json=page_payload,
            headers=_BASE_HEADERS,
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        print(f"\n[NETWORK ERROR] Failed to reach {api_url}")
        print(f"  Exception: {exc}")
        sys.exit(1)

    if response.status_code != 200:
        # AGENT.md Rule 5: halt immediately, dump raw status + headers.
        print(f"\n[HTTP ERROR] Status {response.status_code} from {api_url}")
        print("  Raw response headers:")
        for header_name, header_value in response.headers.items():
            print(f"    {header_name}: {header_value}")
        print(f"\n  Raw response body (first 500 chars):\n  {response.text[:500]}")
        print("\n[HALTED] Non-200 response. No automatic retry. Awaiting manual guidance.")
        sys.exit(response.status_code)

    try:
        return response.json()
    except ValueError as exc:
        print(f"\n[JSON PARSE ERROR] Could not decode response from {api_url}")
        print(f"  Exception: {exc}")
        print(f"  Raw body (first 500 chars):\n  {response.text[:500]}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Pagination engine — Course-correction #3
# ---------------------------------------------------------------------------

def _paginate(
    api_url:      str,
    payload:      dict[str, Any],
    company_name: str,
    base_url:     str,
) -> list[dict[str, str]]:
    """
    Fetch ALL pages for a single company endpoint using `total` to bound the
    pagination loop (Course-correction #3).

    Workday response root structure (from reference scraper):
        {
            "total":       <int>,          ← authoritative page count bound
            "jobPostings": [ {...}, ... ]  ← current page items
        }

    The loop terminates when offset >= total, NOT when a page returns fewer
    items than PAGE_LIMIT. This is safer because Workday can return short pages
    mid-set without signalling the end.
    """
    all_jobs: list[dict[str, str]] = []

    # Page 0 — also discovers `total`
    first_page = _post_page(api_url, payload, offset=0)
    total: int = int(first_page.get("total") or 0)

    print(f"    ← total={total} jobs reported by Workday API")

    raw_postings: list[dict] = first_page.get("jobPostings") or []
    for item in raw_postings:
        all_jobs.append(_normalize_job(item, company_name, base_url))

    if total <= PAGE_LIMIT:
        # Single page — done.
        return all_jobs

    # Remaining pages: offset = PAGE_LIMIT, 2*PAGE_LIMIT, … while < total
    offset = PAGE_LIMIT
    while offset < total:
        print(f"    [delay {INTER_REQUEST_DELAY}s]")  # visible pacing confirmation
        time.sleep(INTER_REQUEST_DELAY)
        page_data = _post_page(api_url, payload, offset=offset)
        raw_postings = page_data.get("jobPostings") or []
        for item in raw_postings:
            all_jobs.append(_normalize_job(item, company_name, base_url))
        offset += PAGE_LIMIT

    return all_jobs


# ---------------------------------------------------------------------------
# Public API — importable entry point (Course-correction #4)
# ---------------------------------------------------------------------------

def fetch_jobs(company: dict[str, Any]) -> list[dict[str, str]]:
    """
    Fetch and normalize all active job listings for a single company config entry.

    Args:
        company: A single company dict from config.yaml, e.g.:
            {
                "name":    "Barclays",
                "ats_type": "workday",
                "api_url": "https://barclays.wd3.myworkdayjobs.com/wday/cxs/.../jobs",
                "payload": { "searchText": "Java", "limit": 20, "offset": 0, ... }
            }

    Returns:
        A list of normalized job dicts ready for CSV/Markdown state tracking.
        Keys: job_id, title, first_discovered_on, last_date, visible,
              relevance, applied, company, url, location, employment_type.

    Raises SystemExit on any non-200 HTTP response or JSON parse failure
    (per AGENT.md Rule 5 — no silent errors, no automatic retry loops).
    """
    name:    str = company["name"]
    api_url: str = company["api_url"]

    # Derive the human-facing base URL from the api_url for building posting links.
    # Pattern: https://{co}.{instance}.myworkdayjobs.com/wday/cxs/{co}/{site}/jobs
    #   → base: https://{co}.{instance}.myworkdayjobs.com/{site}
    base_url: str = api_url  # fallback
    if "/wday/cxs/" in api_url:
        domain_part = api_url.split("/wday/cxs/")[0]          # https://barclays.wd3...
        path_after  = api_url.split("/wday/cxs/")[1]          # barclays/External_.../jobs
        path_parts  = path_after.rstrip("/").split("/")
        # path_parts = [company_slug, site_name, "jobs"]
        if len(path_parts) >= 2:
            site_name = path_parts[1]
            base_url  = f"{domain_part}/{site_name}"

    # Build the POST payload from config, stripping limit/offset
    # (the paginator injects those per-page).
    payload: dict[str, Any] = {
        k: v
        for k, v in (company.get("payload") or {}).items()
        if k not in ("limit", "offset")
    }

    print(f"\n[{name}] Starting Workday scrape")
    print(f"  API URL  : {api_url}")
    print(f"  Base URL : {base_url}")
    print(f"  Payload  : {payload}")

    jobs = _paginate(api_url, payload, name, base_url)

    print(f"\n[{name}] Scrape complete — {len(jobs)} normalized job(s) returned.")
    return jobs


# ---------------------------------------------------------------------------
# Standalone verification entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    print()
    print("=== workday_scraper.py — Task 2: Workday API Scraper Engine ===")
    print(f"    User-Agent : {USER_AGENT}")
    print()

    config  = config_engine.load_config("config.yaml")
    companies = config.get("companies") or []

    if not companies:
        print("[ERROR] No companies found in config.yaml. Halting.")
        sys.exit(1)

    for company in companies:
        if company.get("ats_type", "").lower() != "workday":
            print(f"[SKIP] {company.get('name')} — ats_type is not 'workday'")
            continue

        jobs = fetch_jobs(company)

        print()
        print(f"  {'job_id':<25}  {'title':<55}  location")
        print(f"  {'-'*25}  {'-'*55}  {'-'*30}")
        for job in jobs[:10]:  # Print first 10 for terminal readability
            job_id   = job["job_id"][:24]
            title    = job["title"][:54]
            location = job["location"][:29]
            print(f"  {job_id:<25}  {title:<55}  {location}")

        if len(jobs) > 10:
            print(f"  ... and {len(jobs) - 10} more jobs (truncated for display)")

    print()
    print("[DONE] Task 2 verification complete.")
    print()


if __name__ == "__main__":
    main()
