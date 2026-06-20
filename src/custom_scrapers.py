"""
custom_scrapers.py — Non-Workday ATS Scrapers
----------------------------------------------
Drop-in replacements for workday_scraper.fetch_jobs() for government portals.

Supported ATS types:
  - "dic"  : Digital India Corporation (ora.digitalindiacorporation.in)
  - "nisg" : National Informatics Services Group (myemploywise.com)

Each public function:
  - Accepts a company config dict (same shape as config.yaml companies entries)
  - Returns (list[dict], dict[str, str]):
      list[dict]      — normalized jobs, same keys as workday_scraper._normalize_job()
      dict[str, str]  — {job_id: full_markdown_description}
  - NEVER raises — all exceptions are caught, logged, and return ([], {})

[NET-2.1] Stateful requests.Session() for all HTTP calls.
[NET-2.2] Browser User-Agent on every request.
[NET-2.3] Non-200: warn + return ([], {}) — does not abort batch.
[TECH-1.1] Zero new dependencies — only stdlib + project-approved libraries.
"""

import datetime
import html as html_module
import json
import re
import time
import xml.etree.ElementTree as ET

import html2text
import requests


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_BASE_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

INTER_REQUEST_DELAY: float = 2.0  # polite delay between paginated requests

# DIC
DIC_DASHBOARD_URL: str = "https://ora.digitalindiacorporation.in/dashboard"
DIC_APPLY_URL: str     = "https://ora.digitalindiacorporation.in/"

# NISG
NISG_INIT_URL: str  = "https://www.myemploywise.com/asperm/servlet/website"
NISG_NEXT_URL: str  = "https://www.myemploywise.com/asperm/servlet/ggs.erm.servlet.WebNavigation"
NISG_APPLY_URL: str = "https://www.myemploywise.com/asperm/servlet/website?customer_code=nisg-new"

NISG_INIT_BODY: str = (
    "mode=create&customer_code=nisg-new&viewname=Web"
    "&modulename=Recruitment&pos_id=null&bwi=null"
)
NISG_NEXT_BODY: str = (
    "mode=next&viewname=Web&modulename=Recruitment&customer_code=nisg-new"
)
NISG_CONTENT_HEADERS: dict[str, str] = {
    **_BASE_HEADERS,
    "Content-Type": "application/x-www-form-urlencoded",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _html_to_md(html_text: str) -> str:
    """Convert an HTML string to clean Markdown using html2text."""
    if not html_text or not str(html_text).strip():
        return ""
    h = html2text.HTML2Text()
    h.ignore_links  = False
    h.ignore_images = True
    h.body_width    = 0       # disable 78-char line wrapping
    return h.handle(str(html_text)).strip()


def _make_job_dict(
    job_id:          str,
    title:           str,
    company:         str,
    url:             str,
    location:        str = "",
    last_date:       str = "",
    employment_type: str = "",
    posted_date:     str = "",
) -> dict:
    """
    Return a normalized job dict with all keys expected by state_tracker.py.

    '_external_path' is intentionally blank for custom ATS jobs — these portals
    have no per-job Workday-style detail endpoint. The inline description dict
    (second return value of each fetch function) is used instead.

    'first_discovered_on' is set from the portal's created_at timestamp when
    available, otherwise today's date.
    """
    if posted_date:
        try:
            # Parse ISO timestamp like "2026-06-16T09:20:54.000000Z"
            first_discovered = posted_date[:10]   # take YYYY-MM-DD prefix
        except Exception:
            first_discovered = datetime.date.today().isoformat()
    else:
        first_discovered = datetime.date.today().isoformat()

    return {
        "job_id":              str(job_id),
        "title":               str(title),
        "first_discovered_on": first_discovered,
        "last_date":           str(last_date),
        "visible":             "yes",
        "relevance":           0,
        "applied":             "no",
        "_company":            str(company),
        "_url":                str(url),          # apply/reference URL
        "_location":           str(location),
        "_employment_type":    str(employment_type),
        "_external_path":      "",                # blank = skip detail-fetch loop
    }


# ---------------------------------------------------------------------------
# DIC Scraper
# ---------------------------------------------------------------------------

def fetch_jobs_dic(company: dict) -> tuple[list[dict], dict[str, str]]:
    """
    Fetch all DIC jobs from ora.digitalindiacorporation.in/dashboard.

    Strategy:
      1. GET the dashboard page HTML (single request, no pagination)
      2. Extract all data-job attribute values via regex
      3. Unescape HTML entities (&quot; -> ") and parse as JSON
      4. Build normalized job dicts + Markdown description strings

    The data-job JSON (confirmed live 2026-06-20) contains:
      id, job_title, location, position_type, band (salary/CTC),
      qualification, experience, dep (department), job_description (full HTML),
      last_date, created_at (posting timestamp), total_post, job_category

    Returns:
      (list[dict], dict[str, str]) — jobs and {job_id: markdown_description}
    """
    name = company.get("name", "DIC")
    jobs: list[dict] = []
    descriptions: dict[str, str] = {}

    session = requests.Session()
    session.headers.update(_BASE_HEADERS)

    print(f"\n[{name}] Starting DIC scrape -> {DIC_DASHBOARD_URL}")

    try:
        resp = session.get(DIC_DASHBOARD_URL, timeout=30)
    except requests.exceptions.RequestException as exc:
        print(f"  [{name}] WARN: Network error fetching dashboard: {exc}")
        return [], {}

    if resp.status_code != 200:
        print(f"  [{name}] WARN: Dashboard returned HTTP {resp.status_code}. Skipping.")
        return [], {}

    # Extract all data-job attribute values.
    # Attributes use HTML entities (&quot;) so we unescape before JSON parsing.
    raw_matches = re.findall(r'data-job="([^"]*(?:&quot;[^"]*)*)"', resp.text)
    if not raw_matches:
        # Fallback: single-quoted variant
        raw_matches = re.findall(r"data-job='([^']+)'", resp.text)

    if not raw_matches:
        print(f"  [{name}] WARN: No data-job attributes found. Page structure may have changed.")
        return [], {}

    print(f"  [{name}] Found {len(raw_matches)} data-job elements.")

    for raw in raw_matches:
        try:
            json_str = html_module.unescape(raw)
            job_data = json.loads(json_str)
        except (json.JSONDecodeError, Exception) as exc:
            print(f"  [{name}] WARN: Skipping malformed data-job JSON: {exc}")
            continue

        job_id = str(job_data.get("id", "")).strip()
        if not job_id:
            continue

        title            = str(job_data.get("job_title", "")).strip()
        location         = str(job_data.get("location", "")).strip()
        employment_type  = str(job_data.get("position_type", "")).strip()
        last_date        = str(job_data.get("last_date", "")).strip()
        created_at       = str(job_data.get("created_at", "")).strip()
        band             = str(job_data.get("band", "")).strip()
        qualification    = str(job_data.get("qualification", "")).strip()
        experience       = str(job_data.get("experience", "")).strip()
        department       = str(job_data.get("dep", "")).strip()
        category         = str(job_data.get("job_category", "")).strip()
        total_post       = str(job_data.get("total_post", "")).strip()
        description_html = str(job_data.get("job_description", "")).strip()

        # Build a rich Markdown description with structured metadata + full JD
        meta_lines = []
        if department:
            meta_lines.append(f"**Department:** {department}")
        if category:
            meta_lines.append(f"**Category:** {category}")
        if band:
            meta_lines.append(f"**Compensation:** {band}")
        if qualification:
            meta_lines.append(f"**Qualification:** {qualification}")
        if experience:
            meta_lines.append(f"**Experience:** {experience}")
        if total_post:
            meta_lines.append(f"**Total Posts:** {total_post}")

        description_md_parts = []
        if meta_lines:
            description_md_parts.append("\n".join(meta_lines))
        if description_html:
            description_md_parts.append(_html_to_md(description_html))

        description_md = "\n\n---\n\n".join(description_md_parts)

        jobs.append(_make_job_dict(
            job_id=job_id,
            title=title,
            company=name,
            url=DIC_APPLY_URL,
            location=location,
            last_date=last_date,
            employment_type=employment_type,
            posted_date=created_at,
        ))
        descriptions[job_id] = description_md
        print(f"  [{name}] Parsed: {job_id} — {title}")

    print(f"\n[{name}] DIC scrape complete — {len(jobs)} job(s) found.")
    return jobs, descriptions


# ---------------------------------------------------------------------------
# NISG Scraper
# ---------------------------------------------------------------------------

def _parse_nisg_xml_page(xml_text: str) -> tuple[list[tuple], int]:
    """
    Parse one page of NISG XML response.

    Returns:
      (records, total_count)
      records: list of (job_id, title, last_date, location, description_md) tuples
      total_count: total records reported by portal (0 if not parseable)

    Never raises — returns ([], 0) on any parse failure.
    """
    records: list[tuple] = []
    total = 0

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        print(f"  [NISG] WARN: XML parse error: {exc}")
        return [], 0

    no_rec_node = root.find("no_of_records")
    if no_rec_node is not None and no_rec_node.text:
        try:
            total = int(no_rec_node.text.strip())
        except ValueError:
            pass

    values_node = root.find("values")
    if values_node is None:
        return records, total

    for row_node in values_node:
        display_values = row_node.findall("displayvalue")
        if len(display_values) < 11:
            continue

        try:
            def get_text(idx: int) -> str:
                if idx < len(display_values):
                    return (display_values[idx].text or "").strip()
                return ""

            job_id = get_text(10)
            if not job_id:
                continue

            title     = get_text(2)
            # last_date may include time portion — take only the date
            last_date = get_text(1).split(" ")[0].split("T")[0]
            posted_date = get_text(0).split(" ")[0].split("T")[0]
            location  = get_text(3)

            # Combine all HTML content sections into one Markdown description
            sections = [
                ("## Role Profile",   get_text(4)),
                ("## Skills",         get_text(6)),
                ("## Qualifications", get_text(7)),
                ("## Experience",     get_text(8)),
            ]
            if len(display_values) > 17:
                sections.append(("## Remarks", get_text(17)))

            desc_parts = []
            for heading, html_content in sections:
                md = _html_to_md(html_content)
                if md:
                    desc_parts.append(f"{heading}\n\n{md}")

            description_md = "\n\n".join(desc_parts)
            records.append((job_id, title, last_date, posted_date, location, description_md))

        except Exception as exc:
            print(f"  [NISG] WARN: Skipping malformed row: {exc}")
            continue

    return records, total


def fetch_jobs_nisg(company: dict) -> tuple[list[dict], dict[str, str]]:
    """
    Fetch all NISG jobs from the EmployWise portal.

    Pagination is stateful (cookie-based JSESSIONID, 5 records per page):
      Page 1: POST to NISG_INIT_URL with NISG_INIT_BODY
      Page 2+: POST to NISG_NEXT_URL with NISG_NEXT_BODY
      requests.Session carries the JSESSIONID cookie automatically across requests.

    Infinite-loop guard: tracks seen job_ids; stops if a page adds zero new records.

    Returns:
      (list[dict], dict[str, str]) — jobs and {job_id: markdown_description}
    """
    name = company.get("name", "NISG")
    jobs: list[dict] = []
    descriptions: dict[str, str] = {}
    seen_ids: set[str] = set()

    session = requests.Session()
    session.headers.update(NISG_CONTENT_HEADERS)

    print(f"\n[{name}] Starting NISG scrape -> {NISG_INIT_URL}")

    # Page 1: initial POST
    try:
        resp = session.post(NISG_INIT_URL, data=NISG_INIT_BODY, timeout=30)
    except requests.exceptions.RequestException as exc:
        print(f"  [{name}] WARN: Network error on initial request: {exc}")
        return [], {}

    if resp.status_code != 200:
        print(f"  [{name}] WARN: Initial POST returned HTTP {resp.status_code}. Skipping.")
        return [], {}

    page_records, total = _parse_nisg_xml_page(resp.text)
    print(f"  [{name}] Total records reported by portal: {total}")

    for job_id, title, last_date, posted_date, location, desc_md in page_records:
        if job_id not in seen_ids:
            seen_ids.add(job_id)
            jobs.append(_make_job_dict(
                job_id=job_id, title=title, company=name,
                url=NISG_APPLY_URL, location=location, last_date=last_date,
                posted_date=posted_date,
            ))
            descriptions[job_id] = desc_md

    # Subsequent pages
    fetched = len(page_records)

    while fetched < total:
        time.sleep(INTER_REQUEST_DELAY)
        print(f"  [{name}] Fetching next page (fetched={fetched}/{total})...")

        try:
            resp = session.post(NISG_NEXT_URL, data=NISG_NEXT_BODY, timeout=30)
        except requests.exceptions.RequestException as exc:
            print(f"  [{name}] WARN: Network error on pagination: {exc}. Stopping.")
            break

        if resp.status_code != 200:
            print(f"  [{name}] WARN: Pagination returned HTTP {resp.status_code}. Stopping.")
            break

        page_records, _ = _parse_nisg_xml_page(resp.text)
        if not page_records:
            print(f"  [{name}] WARN: Empty page returned. Stopping pagination.")
            break

        new_on_page = 0
        for job_id, title, last_date, posted_date, location, desc_md in page_records:
            if job_id not in seen_ids:
                seen_ids.add(job_id)
                jobs.append(_make_job_dict(
                    job_id=job_id, title=title, company=name,
                    url=NISG_APPLY_URL, location=location, last_date=last_date,
                    posted_date=posted_date,
                ))
                descriptions[job_id] = desc_md
                new_on_page += 1

        fetched += len(page_records)
        if new_on_page == 0:
            print(f"  [{name}] No new records on this page — stopping to prevent loop.")
            break

    print(f"\n[{name}] NISG scrape complete — {len(jobs)} unique job(s) found.")
    return jobs, descriptions
