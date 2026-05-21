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
  5. fetch_job_detail() exposes per-job description fetching for new listings only.

AGENT.md compliance:
  [TECH-1.2] Pure requests library. No Playwright, Puppeteer, or browser automation.
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

import config_engine

# ---------------------------------------------------------------------------
# Constants — derived from reference/ats-scrapers/src/jobhive/scrapers/workday.py
# ---------------------------------------------------------------------------

# Workday hard-caps `limit` at 20. Sending >20 returns HTTP 400.
PAGE_LIMIT: int = 20

# Polite inter-request delay between paginated POSTs.
# Barclays' Workday edge uses a sliding-window rate limiter that cuts
# rapid bursts. 3 seconds between pages keeps us well under its threshold.
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
    keys align with the master_jobs.csv column schema:

        job_id, title, first_discovered_on, last_date, visible, relevance, applied

    Additional enrichment fields (prefixed with '_') are appended for use by
    state_tracker.py when building .md files. They are stripped by write_ledger()
    before CSV output via extrasaction='ignore'.

    '_external_path' is explicitly included so fetch_job_detail() can construct
    the per-job detail URL without re-parsing.
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
        "first_discovered_on": today,
        "last_date":           today,
        "visible":             "yes",
        "relevance":           "TBD",
        "applied":             "no",

        # --- enrichment fields for Markdown detail files ---
        "_company":        company_name,
        "_url":            url,
        "_location":       location,
        "_employment_type": employment_type,
        "_external_path":  external_path,   # ← required by fetch_job_detail()
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
        response = session.post(
            api_url,
            json=page_payload,
            headers=_BASE_HEADERS,
            timeout=130,
        )
    except requests.exceptions.RequestException as exc:
        print(f"\n[NETWORK ERROR] Failed to reach {api_url}")
        print(f"  Exception: {exc}")
        sys.exit(1)

    if response.status_code != 200:
        print(f"\n[HTTP ERROR] Status {response.status_code} from {api_url}")
        print("  Raw response headers:")
        for k, v in response.headers.items():
            print(f"    {k}: {v}")
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

    A single Session carries PLAY_SESSION cookies across every paginated POST,
    preventing the WAF from treating each request as a new anonymous connection.
    """
    all_jobs: list[dict[str, str]] = []

    session = requests.Session()
    session.headers.update(_BASE_HEADERS)

    # Page 0 — also discovers `total`
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

    Returns a list of normalized job dicts. Each dict includes '_external_path'
    so state_tracker.process_company() can pass it to fetch_job_detail().
    """
    name:    str = company["name"]
    api_url: str = company["api_url"]

    base_url: str = api_url
    if "/wday/cxs/" in api_url:
        domain_part = api_url.split("/wday/cxs/")[0]
        path_after  = api_url.split("/wday/cxs/")[1]
        path_parts  = path_after.rstrip("/").split("/")
        if len(path_parts) >= 2:
            site_name = path_parts[1]
            base_url  = f"{domain_part}/{site_name}"

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
# Public API — fetch_job_detail()
# ---------------------------------------------------------------------------

def fetch_job_detail(
    session:       requests.Session,
    api_url:       str,
    external_path: str,
) -> str | None:
    """
    Fetch the full plain-text job description for a single posting.

    URL construction:
        detail_base = api_url with trailing '/jobs' stripped
        detail_url  = detail_base + external_path
        e.g. https://barclays.wd3.myworkdayjobs.com/wday/cxs/barclays/
                External_Career_Site_Barclays/job/External_Career_Site_Barclays/Title_JR-123

    [NET-2.1] Uses the caller's stateful session — carries PLAY_SESSION cookies
              established during the pagination phase.
    [NET-2.3] Non-200: prints [WARN] with status + headers, returns None.
              Does NOT call sys.exit() — a single failed detail must not abort the batch.

    Text pipeline (derived from reference/ats-scrapers workday.py _extract_description):
        1. Extract raw HTML from jobPostingInfo.jobDescription (or fallbacks)
        2. Strip HTML tags via _TAG_RE
        3. Decode HTML entities via html.unescape() (&amp; → &, &nbsp; → space, etc.)
        4. Collapse whitespace

    Returns clean plain-text string, or None if fetch fails / no description found.
    """
    if not external_path:
        return None

    detail_base = api_url.rsplit("/jobs", 1)[0]
    detail_url  = detail_base + external_path

    print(f"    → GET detail: {detail_url}")

    try:
        response = session.get(detail_url, timeout=30)
    except requests.exceptions.RequestException as exc:
        print(f"    [WARN] Detail fetch network error for {external_path}: {exc}")
        return None

    if response.status_code != 200:
        print(f"    [WARN] Detail endpoint returned HTTP {response.status_code} for {external_path}")
        print(f"      Raw headers: {dict(response.headers)}")
        return None

    try:
        payload = response.json()
    except ValueError:
        print(f"    [WARN] JSON parse failed for detail endpoint: {external_path}")
        return None

    jpi = payload.get("jobPostingInfo") or {}

    # Try known Workday description field names in priority order
    # (from reference/ats-scrapers workday.py _extract_description)
    raw_html: str | None = None
    for key in ("jobDescription", "externalJobDescription", "description"):
        val = jpi.get(key)
        if isinstance(val, str) and val.strip():
            raw_html = val
            break

    if not raw_html:
        return None

    # [TECH-1.4] Production-grade HTML-to-Markdown via html2text.
    # Handles tag stripping, entity decoding (&amp;, &nbsp;, &#39;, etc.),
    # and structure preservation (bullet points, headers, paragraphs) in one pass.
    h = html2text.HTML2Text()
    h.ignore_links  = False   # preserve hyperlinks for AI analysis context
    h.ignore_images = True    # drop image tags — decorative noise in job descriptions
    h.body_width    = 0       # disable 78-char hard-wrap — critical for AI readability

    text = h.handle(raw_html).strip()
    return text if text else None


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
