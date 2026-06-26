"""
eightfold_scraper.py — Eightfold V2 API Scraper Engine
---------------------------------------------------------
Fetches active job listings from Eightfold's V2 global search API.

Endpoint pattern:
    GET https://{api_url}/api/apply/v2/jobs?domain={domain}&start={start}

Key design decisions:
  1. Relies on the standard scraper signature `fetch_jobs(company_cfg)`.
  2. Dynamically loads `location`, `department`, etc. from the `payload` dict in `config.yaml`.
  3. Uses robust exponential backoff for resilience against Eightfold transient 502s.
  4. `fetch_job_detail` fetches the full job description HTML directly using the `{job_id}`.
"""

import datetime
import time
import sys
import logging
from typing import Any

import html2text
import requests

try:
    from . import config_engine
except ImportError:
    import config_engine

logger = logging.getLogger(__name__)

# Eightfold sometimes caps returns, but page limit defaults to 10.
PAGE_LIMIT: int = 10
MAX_RETRIES: int = 3
INITIAL_BACKOFF: float = 2.0


def _clean_html(raw_html: str) -> str:
    """Uses html2text to generate Markdown safely."""
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = True
    converter.body_width = 0
    return converter.handle(raw_html).strip()


def _get_with_retry(session: requests.Session, url: str, params: dict, headers: dict) -> requests.Response:
    """Robust GET with exponential backoff for Eightfold's infamous 429/5xx errors."""
    delay = INITIAL_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        res = session.get(url, params=params, headers=headers, timeout=15)
        if res.status_code in [429, 500, 502, 503, 504]:
            logger.warning(f"[Eightfold] HTTP {res.status_code} on {url}. Retry {attempt}/{MAX_RETRIES} in {delay}s...")
            if attempt == MAX_RETRIES:
                return res
            time.sleep(delay)
            delay *= 2  # exponential backoff
        else:
            return res
    return res


def fetch_jobs(company_cfg: dict[str, Any]) -> list[dict[str, str]]:
    """
    Main paginated GET loop for Eightfold V2.
    """
    company_name = company_cfg["name"]
    api_url = company_cfg["api_url"].rstrip("/")  # e.g., https://hsbc.eightfold.ai
    payload_cfg = company_cfg.get("payload", {})
    
    # Payload provides `domain`, and optional filters like `location`, `department`, `query`.
    domain = payload_cfg.get("domain", "")
    if not domain:
        print(f"[ERROR] Eightfold scraper for {company_name} is missing 'domain' in config payload.", file=sys.stderr)
        return []

    search_endpoint = f"{api_url}/api/apply/v2/jobs"
    
    # We pass all payload params down, overriding/appending 'start' later
    base_params = payload_cfg.copy()
    
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }

    session = requests.Session()
    
    start_idx = 0
    total_expected = -1
    master_list: list[dict[str, str]] = []

    print(f"\n[{company_name}] Starting Eightfold scrape")

    while True:
        params = base_params.copy()
        params["start"] = start_idx
        
        res = _get_with_retry(session, search_endpoint, params=params, headers=headers)
        
        if res.status_code != 200:
            print(f"  [ERROR] {company_name} search returned {res.status_code}.", file=sys.stderr)
            print(f"  Snippet: {res.text[:300]}", file=sys.stderr)
            break

        try:
            data = res.json()
        except Exception as e:
            print(f"  [ERROR] {company_name} returned non-JSON. {e}", file=sys.stderr)
            break

        count = data.get("count", 0)
        positions = data.get("positions", [])

        if start_idx == 0:
            total_expected = count
            if total_expected == 0:
                print(f"  [INFO] {company_name} API reports 0 total jobs.", file=sys.stderr)
                break

        if not positions:
            break

        for job in positions:
            job_id = str(job.get("id", ""))
            if not job_id:
                continue

            # Eightfold uses internal req IDs or ATS job ids.
            req_id = str(job.get("ats_job_id", "")) or job_id
            
            # The direct Eightfold portal URL. (We keep this as standard)
            portal_url = job.get("canonicalPositionUrl", f"{api_url}/careers/job/{job_id}")

            title = str(job.get("name", "Untitled Role")).strip()
            loc = str(job.get("location", "Unknown Location")).strip()
            
            # Derive the posted date
            # t_create/t_update are usually unix timestamps in seconds.
            epoch_sec = job.get("t_create") or job.get("t_update")
            posted_date = ""
            if epoch_sec:
                try:
                    dt = datetime.datetime.fromtimestamp(int(epoch_sec), tz=datetime.timezone.utc)
                    posted_date = dt.strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    pass
            if not posted_date:
                posted_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

            master_list.append({
                "job_id": job_id,
                "title": title,
                "_location": loc,
                "_url": portal_url,
                "req_id": req_id,
                "first_discovered_on": posted_date,
                "relevance": "N",
                "applied": "no",
                "visible": "yes",
                "_external_path": job_id,  # V2 detail endpoint just needs the job_id
                "_apply_url": portal_url   # Will be overridden in the detail fetch if found
            })

        start_idx += len(positions)
        if start_idx >= total_expected:
            break
            
        time.sleep(1.0) # Polite pacing

    return master_list


def fetch_job_detail(session: requests.Session, api_url: str, external_path: str) -> dict[str, str]:
    """
    Given the `external_path` (which we populated with the Eightfold job_id),
    hit `/api/apply/v2/jobs/{job_id}` to retrieve the missing HTML job_description.
    """
    job_id = external_path
    detail_endpoint = f"{api_url}/api/apply/v2/jobs/{job_id}"
    
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    res = _get_with_retry(session, detail_endpoint, params={}, headers=headers)

    if res.status_code != 200:
        logger.warning(f"Eightfold detail fetch failed for {job_id} (HTTP {res.status_code})")
        return {
            "full_description": "Failed to fetch description from Eightfold API.",
            "html_snippet": f"<!-- HTTP {res.status_code} -->"
        }

    try:
        data = res.json()
    except Exception:
        return {"full_description": "Failed to parse JSON."}

    raw_html = str(data.get("job_description", "")).strip()
    if not raw_html:
        # V2 API sometimes includes it in a different block depending on the tenant configuration
        raw_html = "<p>No description provided by Eightfold API.</p>"

    start_date_str = ""
    end_date_str = ""
    try:
        custom_jd = data.get("custom_JD", {}).get("data_fields", {})
        start_list = custom_jd.get("postingStartDate", [])
        end_list = custom_jd.get("postingEndDate", [])
        
        if start_list and start_list[0]:
            try:
                start_date_str = datetime.datetime.strptime(start_list[0], "%d %B %Y").strftime("%Y-%m-%d")
            except ValueError:
                start_date_str = start_list[0]
                
        if end_list and end_list[0]:
            try:
                end_date_str = datetime.datetime.strptime(end_list[0], "%d %B %Y").strftime("%Y-%m-%d")
            except ValueError:
                end_date_str = end_list[0]
    except Exception:
        pass

    return {
        "description": _clean_html(raw_html),
        "start_date": start_date_str,
        "end_date": end_date_str,
        "posted_on": "",
        "exact_location": "",
        "canonical_url": ""
    }
