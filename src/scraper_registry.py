"""
scraper_registry.py
-------------------
Central registry for dynamic scraper dispatch based on `ats_type`.
To add a new ATS, import its module here and add it to the mapping dictionaries.
This cleanly shields `state_tracker.py` from ATS-specific logic.
"""

try:
    from . import workday_scraper
    from . import oracle_scraper
    from . import custom_scrapers
    from . import greenhouse_scraper
    from . import lever_scraper
    from . import eightfold_scraper
    from . import avature_scraper
except ImportError:
    import workday_scraper
    import oracle_scraper
    import custom_scrapers
    import greenhouse_scraper
    import lever_scraper
    import eightfold_scraper
    import avature_scraper

# Standard scrapers must implement:
# - fetch_jobs(company_cfg) -> list[dict]
# - fetch_job_detail(session, api_url, external_path) OR fetch_job_detail(api_url, session, external_path)
# Note: state_tracker currently uses varying signatures for detail fetches. The standard signature is expected moving forward.
_STANDARD_SCRAPERS = {
    "workday": workday_scraper,
    "oracle": oracle_scraper,
    "eightfold": eightfold_scraper,
}

# Custom scrapers must implement:
# - fetch_jobs_*(company_cfg) -> tuple[list[dict], dict] (returning fresh_jobs and inline_descriptions)
_CUSTOM_SCRAPERS = {
    "dic": custom_scrapers.fetch_jobs_dic,
    "nisg": custom_scrapers.fetch_jobs_nisg,
    "greenhouse": greenhouse_scraper.fetch_jobs_greenhouse,
    "lever": lever_scraper.fetch_jobs_lever,
    "avature": avature_scraper.fetch_jobs_avature,
}

def get_standard_scraper(ats_type: str):
    """
    Return the module for a standard scraper, or None if not found.
    """
    return _STANDARD_SCRAPERS.get(ats_type.lower())

def get_custom_fetcher(ats_type: str):
    """
    Return the fetch function for a custom scraper, or None if not found.
    """
    return _CUSTOM_SCRAPERS.get(ats_type.lower())

def is_supported(ats_type: str) -> bool:
    """
    Return True if the ats_type is registered in either dictionary.
    """
    ats_type = ats_type.lower()
    return ats_type in _STANDARD_SCRAPERS or ats_type in _CUSTOM_SCRAPERS
