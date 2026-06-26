"""
greenhouse_scraper.py
---------------------
Custom scraper for Greenhouse ATS.
Returns jobs and inline descriptions in a single pass using the public JSON API.
"""

import requests
import html
import re
from datetime import datetime, timezone
import logging

def _clean_description(value: str) -> str:
    """Unescape HTML and strip tags for plain-text storage."""
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:25000]

def _parse_iso(value: str) -> str:
    """Parse Greenhouse ISO strings to strict ISO 8601 string for CSV."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.isoformat()
    except ValueError:
        return ""

def fetch_jobs_greenhouse(company_cfg: dict) -> tuple[list[dict], dict]:
    """
    Fetch all jobs from Greenhouse public API.
    Returns:
        (fresh_jobs, inline_descriptions)
    """
    company_name = company_cfg.get("name", "Unknown")
    careers_url = company_cfg.get("careers_url", "")
    
    if not careers_url:
        logging.error(f"  [ERROR] No careers_url provided for Greenhouse company {company_name}")
        return [], {}
        
    # Extract slug from URL (e.g., https://boards.greenhouse.io/razorpay -> razorpay)
    slug = careers_url.rstrip("/").split("/")[-1]
    
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    logging.info(f"    → GET {api_url}")
    
    try:
        res = requests.get(api_url, headers=headers, timeout=15)
        res.raise_for_status()
    except requests.RequestException as e:
        logging.error(f"  [ERROR] Failed to fetch Greenhouse jobs for {company_name}: {e}")
        return [], {}
        
    data = res.json()
    jobs = data.get("jobs", [])
    
    fresh_jobs = []
    inline_descriptions = {}
    
    for item in jobs:
        job_id = str(item.get("id", ""))
        if not job_id:
            continue
            
        title = item.get("title", "Unknown Title")
        location = item.get("location", {}).get("name", "")
        url = item.get("absolute_url", "")
        
        # Greenhouse provides the exact posting date
        posted_at = _parse_iso(item.get("first_published")) or _parse_iso(item.get("updated_at"))
        if not posted_at:
            posted_at = datetime.now(timezone.utc).isoformat()
            
        # Optional Requisition ID (Internal ID)
        req_id = item.get("internal_job_id") or ""
            
        job_dict = {
            "job_id": job_id,
            "title": title,
            "location": location,
            "url": url,
            "posted_date": posted_at,
            "last_date": "",  # Greenhouse does not provide deadline on public API
            "req_id": str(req_id)
        }
        
        fresh_jobs.append(job_dict)
        
        # Extract full description
        raw_content = item.get("content", "")
        clean_text = _clean_description(raw_content)
        inline_descriptions[job_id] = {
            "description": clean_text,
            "html": html.unescape(raw_content)  # Keep HTML for rich .md files
        }
        
    logging.info(f"  [{company_name}] Found {len(fresh_jobs)} jobs via Greenhouse API.")
    return fresh_jobs, inline_descriptions
