"""
workday_scraper.py — Task 2: Workday API Scraper Engine
---------------------------------------------------------
Fetches active job listings from Workday's undocumented but stable CXS API.

Endpoint pattern (derived from ./reference/ats-scrapers reference):
    POST https://{company}.{instance}.myworkdayjobs.com/wday/cxs/{company}/{site}/jobs

Key design decisions:
  1. fetch_jobs() returns normalized flat dicts aligned with master_jobs.csv columns.
  2. Realistic browser User-Agent header on every request [NET-2.2].
  3. Pagination bounded by `total` integer in Workday response root [NET-2.1].
  4. Fully importable module — no side effects at import time.
  5. fetch_job_detail() returns a rich dict for deep metadata + description extraction.

AGENT.md compliance:
  [TECH-1.2] Pure requests library. No Playwright, Puppeteer, or browser automation.
  [TECH-1.4] html2text authorized for production-grade HTML-to-Markdown conversion.
  [NET-2.1]  Stateful session across all paginated POSTs and detail GETs.
  [NET-2.2]  Browser User-Agent on every request.
  [NET-2.3]  Non-200 on search: halt + dump headers. Non-200 on detail: warn + continue.
"""

import datetime
import sys
import time
from typing import Any

import html2text
import requests

try:
    from . import config_engine          # when imported as part of the src package
except ImportError:
    import config_engine                 # when run directly: python src/workday_scraper.py

# ---------------------------------------------------------------------------
# Constants — derived from reference/ats-scrapers/src/jobhive/scrapers/workday.py
# ---------------------------------------------------------------------------

# Workday hard-caps `limit` at 20. Sending >20 returns HTTP 400.
PAGE_LIMIT: int = 20

# Polite inter-request delay between paginated POSTs.
INTER_REQUEST_DELAY: float = 3.0

# A realistic browser User-Agent avoids instant 403s from corporate firewalls.
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

# html2text converter is instantiated per-call inside fetch_job_detail().
# [TECH-1.4] — authorized production-grade HTML-to-Markdown conversion library.


# ---------------------------------------------------------------------------
# Schema normalization
# ---------------------------------------------------------------------------

def _normalize_job(raw_item: dict[str, Any], company_name: str, base_url: str) -> dict[str, str]:
    """
    Map a single raw Workday job posting dict into a flat normalized dict whose
    keys align with the master_jobs.csv column schema.

    '_external_path' is explicitly included so fetch_job_detail() can construct
    the per-job detail URL without re-parsing.
    """
    external_path: str = raw_item.get("externalPath") or ""

    bullet_fields: list = raw_item.get("bulletFields") or []
    bullet_id: str | None = bullet_fields[0] if bullet_fields else None
    job_id: str = (
        bullet_id
        or (external_path.rsplit("/", 1)[-1] if external_path else "")
        or "unknown"
    )

    title: str         = (raw_item.get("title") or "Untitled").strip()
    url: str           = f"{base_url}{external_path}" if external_path else base_url
    location: str      = (raw_item.get("locationsText") or "").strip()
    employment_type: str = (raw_item.get("timeType") or "").strip()
    today: str         = datetime.date.today().isoformat()

    return {
        # --- master_jobs.csv required columns ---
        "job_id":              job_id,
        "title":               title,
        "first_discovered_on": today,
        "last_date":           today,
        "visible":             "yes",
        "relevance":           0,       # numeric score 0–100; set by LLM pipeline later
        "applied":             "no",
        # --- enrichment fields (stripped from CSV by extrasaction='ignore') ---
        "_company":           company_name,
        "_url":               url,
        "_location":          location,
        "_employment_type":   employment_type,
        "_external_path":     external_path,
    }


# ---------------------------------------------------------------------------
# Core API call — single page
# ---------------------------------------------------------------------------

def _post_page(
    api_url:  str,
    payload:  dict[str, Any],
    offset:   int,
    session:  requests.Session,
) -> dict[str, Any]:
    """
    POST one paginated request to the Workday CXS jobs endpoint.
    [NET-2.3] Non-200: immediately halt, dump raw status + headers. No auto-retry.
    """
    page_payload = {**payload, "limit": PAGE_LIMIT, "offset": offset}
    print(f"    → POST {api_url}  offset={offset}")

    try:
        response = session.post(api_url, json=page_payload, headers=_BASE_HEADERS, timeout=130)
    except requests.exceptions.RequestException as exc:
        print(f"\n[NETWORK ERROR] Failed to reach {api_url}")
        print(f"  Exception: {exc}")
        raise RuntimeError(f"Network error: {exc}")

    if response.status_code != 200:
        print(f"\n[HTTP ERROR] Status {response.status_code} from {api_url}")
        print("  Raw response headers:")
        for k, v in response.headers.items():
            print(f"    {k}: {v}")
        print(f"\n  Raw response body (first 500 chars):\n  {response.text[:500]}")
        print("\n[HALTED] Non-200 response. No automatic retry.")
        raise RuntimeError(f"HTTP {response.status_code} from {api_url}")

    try:
        return response.json()
    except ValueError as exc:
        print(f"\n[JSON PARSE ERROR] Could not decode response from {api_url}")
        print(f"  Exception: {exc}")
        print(f"  Raw body (first 500 chars):\n  {response.text[:500]}")
        raise RuntimeError(f"JSON decode error: {exc}")


# ---------------------------------------------------------------------------
# Pagination engine
# ---------------------------------------------------------------------------

def _paginate(
    api_url:      str,
    payload:      dict[str, Any],
    company_name: str,
    base_url:     str,
) -> list[dict[str, str]]:
    """
    Fetch ALL pages using `total` to bound the loop [NET-2.1].
    A single Session carries PLAY_SESSION cookies across every paginated POST.
    """
    all_jobs: list[dict[str, str]] = []

    session = requests.Session()
    session.headers.update(_BASE_HEADERS)

    first_page = _post_page(api_url, payload, offset=0, session=session)
    total: int = int(first_page.get("total") or 0)
    print(f"    ← total={total} jobs reported by Workday API")

    for item in (first_page.get("jobPostings") or []):
        all_jobs.append(_normalize_job(item, company_name, base_url))

    if total <= PAGE_LIMIT:
        return all_jobs

    offset = PAGE_LIMIT
    while offset < total:
        print(f"    [delay {INTER_REQUEST_DELAY}s]")
        time.sleep(INTER_REQUEST_DELAY)
        page_data = _post_page(api_url, payload, offset=offset, session=session)
        for item in (page_data.get("jobPostings") or []):
            all_jobs.append(_normalize_job(item, company_name, base_url))
        offset += PAGE_LIMIT

    return all_jobs


# ---------------------------------------------------------------------------
# Public API — fetch_jobs()
# ---------------------------------------------------------------------------

def fetch_jobs(company: dict[str, Any]) -> list[dict[str, str]]:
    """
    Fetch and normalize all active job listings for a single company config entry.
    Each returned dict includes '_external_path' for use by fetch_job_detail().
    """
    name:    str = company["name"]
    api_url: str = company["api_url"]

    base_url: str = api_url
    if "/wday/cxs/" in api_url:
        domain_part = api_url.split("/wday/cxs/")[0]
        path_after  = api_url.split("/wday/cxs/")[1]
        path_parts  = path_after.rstrip("/").split("/")
        if len(path_parts) >= 2:
            base_url = f"{domain_part}/{path_parts[1]}"

    payload: dict[str, Any] = {
        k: v for k, v in (company.get("payload") or {}).items()
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
# Public API — fetch_job_detail()
# ---------------------------------------------------------------------------

def fetch_job_detail(
    session:       requests.Session,
    api_url:       str,
    external_path: str,
) -> dict | None:
    """
    Fetch rich metadata and full job description for a single posting.

    URL construction:
        detail_base = api_url with trailing '/jobs' stripped
        detail_url  = detail_base + external_path

    Returns a dict with keys:
        description    — html2text Markdown body [TECH-1.4]
        start_date     — authoritative HR posting date (jpi.startDate, ISO format)
        posted_on      — relative age string (jpi.postedOn, "Posted 30+ Days Ago")
        exact_location — precise location from detail page (jpi.location)
        canonical_url  — Workday canonical URL (jpi.externalUrl or constructed URL)

    Returns None ONLY on network failure or JSON parse error.
    Missing individual fields land as "" — never crash on absent data.

    [NET-2.1] Uses caller's stateful session — carries PLAY_SESSION cookies.
    [NET-2.3] Non-200: prints [WARN] + headers, returns None (does NOT sys.exit).
    """
    if not external_path:
        return None

    detail_base   = api_url.rsplit("/jobs", 1)[0]
    detail_url    = detail_base + external_path
    canonical_url = detail_url   # baseline fallback before parsing response

    print(f"    → GET detail: ...{external_path[-60:]}")

    try:
        response = session.get(detail_url, timeout=30)
    except requests.exceptions.RequestException as exc:
        print(f"    [WARN] Detail fetch network error for {external_path}: {exc}")
        return None

    if response.status_code != 200:
        print(f"    [WARN] Detail endpoint returned HTTP {response.status_code}")
        print(f"      Raw headers: {dict(response.headers)}")
        return None

    try:
        payload = response.json()
    except ValueError:
        print(f"    [WARN] JSON parse failed for detail endpoint: {external_path}")
        return None

    jpi = payload.get("jobPostingInfo") or {}

    # --- Description: try known field names in priority order ---
    raw_html: str | None = None
    for key in ("jobDescription", "externalJobDescription", "description"):
        val = jpi.get(key)
        if isinstance(val, str) and val.strip():
            raw_html = val
            break

    description = ""
    if raw_html:
        h = html2text.HTML2Text()
        h.ignore_links  = False   # preserve hyperlinks for AI analysis
        h.ignore_images = True    # drop decorative image tags
        h.body_width    = 0       # disable 78-char hard-wrap
        description = h.handle(raw_html).strip()

    # --- Rich metadata fields ---
    start_date     = str(jpi.get("startDate")   or "").strip()
    end_date       = str(jpi.get("endDate")     or "").strip()  # HR application deadline
    posted_on      = str(jpi.get("postedOn")    or "").strip()
    exact_location = str(jpi.get("location")    or "").strip()
    external_url   = str(jpi.get("externalUrl") or "").strip()
    if external_url:
        canonical_url = external_url

    return {
        "description":    description,
        "start_date":     start_date,
        "end_date":       end_date,       # application closing date
        "posted_on":      posted_on,
        "exact_location": exact_location,
        "canonical_url":  canonical_url,
    }


# ---------------------------------------------------------------------------
# Standalone verification entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    print()
    print("=== workday_scraper.py — Task 2: Workday API Scraper Engine ===")
    print(f"    User-Agent : {USER_AGENT}")
    print()

    config    = config_engine.load_config("config.yaml")
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
        for job in jobs[:10]:
            print(
                f"  {job['job_id'][:24]:<25}  "
                f"{job['title'][:54]:<55}  "
                f"{job['_location'][:29]}"
            )
        if len(jobs) > 10:
            print(f"  ... and {len(jobs) - 10} more jobs (truncated for display)")

    print()
    print("[DONE] Task 2 verification complete.")
    print()


if __name__ == "__main__":
    main()
