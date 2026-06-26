"""
oracle_scraper.py
-----------------
Oracle Recruiting Cloud API integration for the Job Application OS.
Extracts paginated job listings and HTML detail metadata via REST endpoints.
"""

import datetime
import html2text
import requests
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_BASE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": USER_AGENT,
}

PAGE_LIMIT = 200

def _normalize_job(raw_item: dict[str, Any], company_name: str, base_url: str, site_number: str) -> dict[str, Any]:
    """
    Map a single Oracle API job dict to our standardized schema.
    Returns the required master_jobs.csv columns + enrichment fields.
    """
    job_id = str(raw_item.get("Id") or raw_item.get("RequisitionNumber") or "")
    title = str(raw_item.get("Title") or "Untitled").strip()
    
    # URL construction: default fallback URL
    job_url = raw_item.get("ExternalURL")
    if not job_url:
        job_url = f"{base_url}/?keyword=&mode=jobs&lang=en&site_number={site_number}#{job_id}"
        
    location = str(raw_item.get("PrimaryLocation") or "").strip()
    
    # Employment Type extraction
    employment_type = ""
    for key in ("WorkerType", "JobType", "ContractType", "JobSchedule"):
        val = raw_item.get(key)
        if isinstance(val, str) and val.strip():
            employment_type = val.strip()
            break
            
    # Dates
    first_date = str(raw_item.get("PostedDate") or raw_item.get("CreatedOn") or "").split("T")[0]
    if not first_date:
        first_date = datetime.date.today().isoformat()
        
    return {
        # --- master_jobs.csv required columns ---
        "job_id":              job_id,
        "title":               title,
        "first_discovered_on": first_date,
        "last_date":           "",      # Usually no strict deadline exposed in list payload
        "visible":             "yes",
        "relevance":           0,       # numeric score 0-100; set by LLM pipeline later
        "applied":             "no",
        # --- enrichment fields (stripped from CSV by extrasaction='ignore') ---
        "_company":           company_name,
        "_url":               job_url,
        "_location":          location,
        "_employment_type":   employment_type,
        "_external_path":     job_id,   # For Oracle, the external path is just the ID to feed the detail API
    }


def fetch_jobs(company_cfg: dict) -> list[dict]:
    """
    Fetch all jobs for an Oracle ATS company.
    Requires `api_url` in config.yaml to point to the base URL 
    (e.g., https://jpmc.fa.oraclecloud.com).
    """
    company_name = company_cfg.get("name", "Unknown")
    base_url = company_cfg.get("api_url", "").rstrip("/")
    if not base_url:
        print(f"  [ERROR] Oracle scraper requires 'api_url' (e.g. https://...oraclecloud.com) for {company_name}")
        return []
        
    print(f"\n[{company_name}] Starting Oracle scrape")
    
    payload = company_cfg.get("payload", {})
    site_number = payload.get("siteNumber", "CX_1")
    
    # Build finder string from payload arguments
    # Ex: limit=25,offset=0,keyword=java,locationId=...
    finder_args = [f"siteNumber={site_number}"]
    
    for k, v in payload.items():
        if k not in ["siteNumber", "limit", "offset"]:
            finder_args.append(f"{k}={v}")
            
    api_url = f"{base_url}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    all_jobs = []
    seen = set()
    offset = 0
    
    with requests.Session() as session:
        while True:
            current_finder = f"findReqs;{','.join(finder_args)},limit={PAGE_LIMIT},offset={offset}"
            params = {
                "onlyData": "true",
                "expand": "requisitionList",
                "finder": current_finder,
            }
            
            print(f"    → GET {api_url}  offset={offset}")
            try:
                response = session.get(api_url, params=params, headers=_BASE_HEADERS, timeout=60)
            except requests.exceptions.RequestException as exc:
                print(f"  [NETWORK ERROR] Failed to reach Oracle API: {exc}")
                break
                
            if response.status_code != 200:
                print(f"  [HTTP ERROR] Status {response.status_code} from {api_url}")
                break
                
            try:
                data = response.json()
            except ValueError:
                print("  [JSON PARSE ERROR] Could not decode Oracle response")
                break
                
            items = data.get("items", [])
            if not items or not isinstance(items[0], dict):
                break
                
            reqs = items[0].get("requisitionList")
            if not isinstance(reqs, list) or not reqs:
                break
                
            for raw_item in reqs:
                job = _normalize_job(raw_item, company_name, base_url, site_number)
                if job["job_id"] not in seen:
                    seen.add(job["job_id"])
                    all_jobs.append(job)
                    
            if len(reqs) < PAGE_LIMIT:
                break
                
            offset += PAGE_LIMIT
            
    print(f"\n[{company_name}] Scrape complete - {len(all_jobs)} normalized job(s) returned.\n")
    return all_jobs


def fetch_job_detail(session: requests.Session, api_url: str, job_id: str) -> dict[str, str] | None:
    """
    Fetch the full HTML job description and parse into Markdown.
    `api_url` is the base URL (e.g. https://...oraclecloud.com).
    `job_id` comes from `_external_path`.
    """
    if not job_id:
        return None
        
    base_url = api_url.rstrip("/")
    detail_url = f"{base_url}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
    
    params = {
        "finder": f"ById;Id={job_id}",
        "onlyData": "true"
    }
    
    print(f"    -> GET detail: {job_id}")
    
    try:
        response = session.get(detail_url, params=params, headers=_BASE_HEADERS, timeout=30)
    except requests.exceptions.RequestException as exc:
        print(f"    [WARN] Oracle detail network error for {job_id}: {exc}")
        return None
        
    if response.status_code != 200:
        return None
        
    try:
        data = response.json()
    except ValueError:
        return None
        
    items = data.get("items", [])
    if not items or not isinstance(items[0], dict):
        return None
        
    detail = items[0]
    
    # Concatenate standard Oracle sections
    parts = []
    for key in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"):
        val = detail.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
            
    raw_html = "\n<br>\n".join(parts)
    
    description = ""
    if raw_html:
        h = html2text.HTML2Text()
        h.ignore_links  = False
        h.ignore_images = True
        h.body_width    = 0
        description = h.handle(raw_html).strip()
        
    start_date = ""
    posted_start = detail.get("ExternalPostedStartDate")
    if posted_start:
        start_date = str(posted_start).split("T")[0]
        
    end_date = ""
    posted_end = detail.get("ExternalPostedEndDate")
    if posted_end:
        end_date = str(posted_end).split("T")[0]
        
    return {
        "description": description,
        "start_date": start_date,
        "end_date": end_date,
        "posted_on": "",
        "exact_location": "",
        "canonical_url": "",
    }
