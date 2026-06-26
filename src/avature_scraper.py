"""
avature_scraper.py
------------------
Custom scraper for Avature ATS.
Avature embeds jobs in HTML and requires per-job HTML fetching for details.
This custom scraper uses ThreadPoolExecutor to concurrently fetch all details 
and returns them as inline_descriptions to keep `state_tracker.py` fast.
"""

import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime, timezone
import concurrent.futures
from urllib.parse import urljoin
import logging

logger = logging.getLogger(__name__)

PAGE_SIZE = 12
MAX_PAGES = 50

_PSEUDO_TITLES = {
    "apply", "apply now", "apply online", "learn more", "view job",
    "view all", "see job", "more info", "details",
}

def _parse_job_element(element, anchor, base_url):
    href = (anchor.get("href") or "").strip()
    if not href or "/JobDetail/" not in href:
        return None

    url = urljoin(base_url, href)

    # Job ID = last non-empty path segment (strip query string).
    ats_id = href.rsplit("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if not ats_id:
        return None

    # Title preference order
    title = ""
    title_el = (
        element.find(["h2", "h3"])
        or element.find(class_=lambda v: bool(v) and "title" in str(v).lower())
    )
    if title_el:
        title = title_el.get_text(strip=True)
    if not title:
        anchor_text = anchor.get_text(strip=True)
        if anchor_text.lower() not in _PSEUDO_TITLES:
            title = anchor_text
            
    title = re.sub(r"\s+", " ", title).strip()
    if not title or title.lower() in _PSEUDO_TITLES:
        return None

    # Location
    location = ""
    loc_el = element.find(class_=lambda v: bool(v) and "location" in str(v).lower())
    if loc_el:
        location = re.sub(r"\s+", " ", loc_el.get_text(strip=True)).strip()

    return {
        "job_id": ats_id,
        "title": title,
        "_location": location,
        "_url": url,
        "first_discovered_on": datetime.now(timezone.utc).date().isoformat()
    }

def _parse_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    description_parts = []

    for blk in soup.find_all(class_="article--details"):
        field_rows = [
            d for d in blk.find_all("div")
            if "article__content__view__field" in (d.get("class") or [])
        ]
        labeled_count = 0
        for fr in field_rows:
            lbl_el = fr.find("div", class_="article__content__view__field__label")
            val_el = fr.find("div", class_="article__content__view__field__value")
            label_text = lbl_el.get_text(strip=True) if lbl_el else ""
            if label_text:
                labeled_count += 1
                if val_el:
                    value_text = re.sub(r"\s+", " ", val_el.get_text(" ", strip=True))
                    if value_text:
                        fields[label_text.lower().rstrip(":")] = value_text

        if labeled_count >= 2:
            continue

        body_added = False
        for fr in field_rows:
            lbl_el = fr.find("div", class_="article__content__view__field__label")
            if lbl_el and lbl_el.get_text(strip=True):
                continue
            val_el = fr.find("div", class_="article__content__view__field__value") or fr
            text = val_el.get_text(separator="\n", strip=True)
            if text:
                description_parts.append(text)
                body_added = True
                
        if not field_rows and not body_added:
            content = blk.find("div", class_="article__content") or blk
            text = content.get_text(separator="\n", strip=True)
            if text and len(text) > 100:
                description_parts.append(text)

    description = "\n\n".join(description_parts).strip()
    description = re.sub(r"\n{3,}", "\n\n", description)
    return {"fields": fields, "description": description}

def _fetch_one_detail(url: str) -> dict:
    import httpcloak
    _BROWSER_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        r = httpcloak.get(url, headers=_BROWSER_HEADERS, timeout=30)
        if r.status_code == 200:
            return _parse_detail(r.text)
    except:
        pass
    return {"fields": {}, "description": ""}

def fetch_jobs_avature(company_cfg: dict) -> tuple[list[dict], dict]:
    """
    Fetch all jobs from Avature HTML portal concurrently.
    Returns:
        (fresh_jobs, inline_descriptions)
    """
    api_url = company_cfg.get("api_url", "")
    payload = company_cfg.get("payload", {})
    
    if not api_url:
        logger.error("[Avature] Missing api_url in company config.")
        return [], {}

    import httpcloak

    _BROWSER_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    seen = set()
    fresh_jobs = []
    
    company_name = company_cfg.get("name", "Avature")
    logger.info(f"  [Avature] Fetching live jobs for: {company_name}")

    for page_num in range(MAX_PAGES):
        offset = page_num * PAGE_SIZE
        # Overwrite/append offset into the payload
        req_params = dict(payload)
        req_params["jobOffset"] = offset
        req_params["jobRecordsPerPage"] = PAGE_SIZE

        try:
            r = httpcloak.get(api_url, params=req_params, headers=_BROWSER_HEADERS, timeout=30)
        except Exception as e:
            logger.warning(f"  [Avature] Request failed at offset {offset}: {e}")
            break
            
        if r.status_code != 200:
            logger.warning(f"  [Avature] Bad status {r.status_code} at offset {offset}")
            break

        soup = BeautifulSoup(r.text, "html.parser")
        anchors = soup.find_all("a", href=lambda h: bool(h) and "/JobDetail/" in h)
        
        page_jobs = []
        for anchor in anchors:
            container = anchor.find_parent(["article", "li", "tr"]) or anchor.find_parent(
                "div",
                class_=lambda v: bool(v) and any(
                    k in str(v).lower() for k in ("job", "result", "listing", "article")
                ),
            )
            element = container or anchor
            job = _parse_job_element(element, anchor, api_url)
            if job and job["job_id"] not in seen:
                seen.add(job["job_id"])
                page_jobs.append(job)
                
        fresh_jobs.extend(page_jobs)
        
        # Stop if page is empty or smaller than page size
        if len(page_jobs) < PAGE_SIZE:
            break

    logger.info(f"  [Avature] Found {len(fresh_jobs)} list items. Fetching details concurrently...")

    # Concurrently fetch JobDetail pages to get descriptions
    inline_descriptions = {}
    
    def worker(job):
        detail = _fetch_one_detail(job["_url"])
        return job["job_id"], detail
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_job = {executor.submit(worker, job): job for job in fresh_jobs}
        for future in concurrent.futures.as_completed(future_to_job):
            try:
                job_id, detail = future.result()
                if detail.get("description"):
                    inline_descriptions[job_id] = detail["description"]
                    
                # Optionally parse 'posted date' from fields if available
                # Bloomberg might use "date published" or similar
                posted_labels = ["date published", "posted date", "publication date", "post date", "date posted"]
                fields = detail.get("fields", {})
                for lbl in posted_labels:
                    if lbl in fields:
                        # Attempt to parse date or just stash it in first_discovered_on 
                        # if we were being rigorous. We'll leave it simple for now.
                        pass
                        
            except Exception as e:
                logger.debug(f"  [Avature] Detail fetch error: {e}")

    logger.info(f"  [Avature] Detail fetching complete. Found {len(inline_descriptions)} descriptions.")
    return fresh_jobs, inline_descriptions
