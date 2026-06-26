"""
lever_scraper.py
----------------
Custom scraper for Lever ATS.
Returns jobs and inline descriptions in a single pass using the public JSON API.
"""

import requests
import html
import re
from datetime import datetime, timezone
import logging

def _clean_description(value: str) -> str:
    """Strip tags for plain-text storage."""
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:25000]

def _parse_epoch(value: int) -> str:
    """Parse Lever millisecond epoch to ISO 8601 string for CSV."""
    if not value:
        return ""
    try:
        dt = datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError):
        return ""

def fetch_jobs_lever(company_cfg: dict) -> tuple[list[dict], dict]:
    """
    Fetch all jobs from Lever public API.
    Returns:
        (fresh_jobs, inline_descriptions)
    """
    company_name = company_cfg.get("name", "Unknown")
    careers_url = company_cfg.get("careers_url", "")
    
    if not careers_url:
        logging.error(f"  [ERROR] No careers_url provided for Lever company {company_name}")
        return [], {}
        
    # Extract slug from URL (e.g., https://jobs.lever.co/alpha-grep -> alpha-grep)
    slug = careers_url.rstrip("/").split("/")[-1]
    
    api_url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    logging.info(f"    → GET {api_url}")
    
    try:
        res = requests.get(api_url, headers=headers, timeout=15)
        res.raise_for_status()
    except requests.RequestException as e:
        logging.error(f"  [ERROR] Failed to fetch Lever jobs for {company_name}: {e}")
        return [], {}
        
    jobs = res.json()
    
    fresh_jobs = []
    inline_descriptions = {}
    
    for item in jobs:
        job_id = str(item.get("id", ""))
        if not job_id:
            continue
            
        title = item.get("text", "Unknown Title")
        location = item.get("categories", {}).get("location", "")
        url = item.get("hostedUrl", "")
        
        # Lever provides createdAt as millisecond epoch
        posted_at = _parse_epoch(item.get("createdAt"))
        if not posted_at:
            posted_at = datetime.now(timezone.utc).isoformat()
            
        # Optional Requisition ID
        req_id = item.get("reqId") or ""
            
        job_dict = {
            "job_id": job_id,
            "title": title,
            "location": location,
            "url": url,
            "posted_date": posted_at,
            "last_date": "",  # Lever does not provide deadline
            "req_id": str(req_id)
        }
        
        fresh_jobs.append(job_dict)
        
        # Extract full description
        # Lever provides descriptionPlain natively
        raw_text = item.get("descriptionPlain", "")
        clean_text = _clean_description(raw_text)
        
        # We can construct HTML from their description HTML to make it rich
        html_desc = item.get("description", "")
        # Also append lists (responsibilities, requirements) which they often separate
        lists = item.get("lists", [])
        for lst in lists:
            if lst.get("text"):
                html_desc += f"<h3>{lst['text']}</h3>"
            html_desc += "<ul>"
            html_desc += lst.get("content", "")
            html_desc += "</ul>"
            
        inline_descriptions[job_id] = {
            "description": clean_text,
            "html": html_desc
        }
        
    logging.info(f"  [{company_name}] Found {len(fresh_jobs)} jobs via Lever API.")
    return fresh_jobs, inline_descriptions
