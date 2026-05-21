"""
state_tracker.py — Task 3: State Tracking & File Writer Module (v2)
--------------------------------------------------------------------
Implements the deduplication routine against the master_jobs.csv ledger,
writes per-job Markdown detail files, and fetches full job descriptions
for newly discovered postings only.

Three-way reconciliation logic:
  - NEW job_id     → append full row to CSV, fetch description, create job_<id>.md
  - EXISTING id    → update last_date + visible=yes; preserve first_discovered_on,
                     relevance, applied; do NOT re-fetch description or overwrite .md
  - DELISTED id    → mark visible=no in CSV; leave .md file intact

Safety guardrail [user-confirmed]:
  Description detail endpoint is ONLY called for brand-new job_ids.
  Existing entries never trigger a detail fetch, eliminating unnecessary
  network signatures against the corporate WAF.

Rules cited:
  [EXEC-4.1]  Single task scope.
  [EXEC-4.3]  Done when terminal confirms CSV rows + .md files on disk.
  [NET-2.1]   Stateful requests.Session() for all detail GETs.
  [NET-2.3]   Non-200 on detail: warn + continue (does not abort batch).
  [SEC-3.2]   All writes go to ./targets/[company]/ paths only.
  [TECH-1.1]  Pure Python 3.14 stdlib + project-local imports.

Usage:
    python state_tracker.py
Import:
    from state_tracker import process_company
"""

import csv
import datetime
import re
import sys
from pathlib import Path
from typing import Any

import requests

import config_engine
import workday_scraper

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Exact CSV column order required by project_description.md schema contract.
CSV_COLUMNS: list[str] = [
    "job_id",
    "title",
    "first_discovered_on",
    "last_date",
    "visible",
    "relevance",
    "applied",
]

# Windows-illegal filename characters — replaced with '_' in .md filenames.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


# ---------------------------------------------------------------------------
# Function 1 — Load existing CSV ledger
# ---------------------------------------------------------------------------

def load_ledger(csv_path: Path) -> dict[str, dict]:
    """
    Read master_jobs.csv into a dict keyed by job_id.
    Returns an empty dict on first run — absence is a valid state, not an error.
    """
    if not csv_path.exists():
        return {}

    ledger: dict[str, dict] = {}
    with csv_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            job_id = row.get("job_id", "").strip()
            if job_id:
                ledger[job_id] = dict(row)

    return ledger


# ---------------------------------------------------------------------------
# Function 2 — Reconcile ledger against fresh scrape
# ---------------------------------------------------------------------------

def reconcile(
    ledger:     dict[str, dict],
    fresh_jobs: list[dict],
) -> tuple[dict[str, dict], dict[str, Any]]:
    """
    Apply the three-way deduplication logic.

    MODIFICATION (user-confirmed [EXEC-4.2]):
    new_jobs list is explicitly populated inside this function and returned
    via the summary payload so process_company() can iterate over it for
    detail fetching. Each entry in new_jobs carries ALL enrichment fields
    from the scraper, including '_external_path', which fetch_job_detail()
    requires to construct the per-job detail URL.

    Returns:
        (updated_ledger, summary)
        summary keys: new (int), updated (int), delisted (int), new_jobs (list[dict])
    """
    today:     str       = datetime.date.today().isoformat()
    fresh_ids: set[str]  = {job["job_id"] for job in fresh_jobs}

    new_count:      int       = 0
    updated_count:  int       = 0
    delisted_count: int       = 0
    new_jobs:       list[dict] = []

    # --- Pass 1: process every job in the fresh pull ---
    for job in fresh_jobs:
        job_id = job["job_id"]

        if job_id not in ledger:
            # NEW — build the full ledger row and explicitly include all
            # enrichment fields needed downstream (including _external_path).
            ledger[job_id] = {
                "job_id":              job_id,
                "title":               job.get("title", ""),
                "first_discovered_on": today,
                "last_date":           today,
                "visible":             "yes",
                "relevance":           job.get("relevance", "TBD"),
                "applied":             job.get("applied", "no"),
                # Enrichment fields — stripped from CSV by extrasaction='ignore'
                "_company":           job.get("_company", ""),
                "_url":               job.get("_url", ""),
                "_location":          job.get("_location", ""),
                "_employment_type":   job.get("_employment_type", ""),
                "_external_path":     job.get("_external_path", ""),  # ← for detail fetch
            }
            new_count += 1
            new_jobs.append(ledger[job_id])   # ← explicitly populated in reconcile()

        else:
            # EXISTING — update activity fields only; preserve user-managed values
            ledger[job_id]["last_date"] = today
            ledger[job_id]["visible"]   = "yes"
            updated_count += 1

    # --- Pass 2: delist any ledger entry absent from the fresh pull ---
    for job_id, row in ledger.items():
        if job_id not in fresh_ids:
            if row.get("visible") != "no":
                row["visible"] = "no"
                delisted_count += 1

    summary = {
        "new":      new_count,
        "updated":  updated_count,
        "delisted": delisted_count,
        "new_jobs": new_jobs,         # ← consumed by process_company() detail loop
    }
    return ledger, summary


# ---------------------------------------------------------------------------
# Function 3 — Write CSV ledger
# ---------------------------------------------------------------------------

def write_ledger(ledger: dict[str, dict], csv_path: Path) -> None:
    """
    Persist the reconciled ledger to master_jobs.csv.

    Enforces exact column order: job_id, title, first_discovered_on,
    last_date, visible, relevance, applied.
    All '_'-prefixed enrichment keys are silently dropped (extrasaction='ignore').
    """
    with csv_path.open(mode="w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in ledger.values():
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Function 4 — Write per-job Markdown detail file
# ---------------------------------------------------------------------------

def _sanitize_filename(job_id: str) -> str:
    """Replace Windows-illegal filename characters with '_'."""
    return _ILLEGAL_FILENAME_CHARS.sub("_", job_id)


def write_job_detail(
    job:             dict,
    job_details_dir: Path,
    description:     str | None = None,
) -> None:
    """
    Create job_<id>.md inside targets/[company]/job_details/.

    Called ONLY for new jobs — never overwrites existing files.
    When 'description' is provided (fetched from Workday detail endpoint),
    it is appended under a '## Job Description' header for AI CV-matching.
    When None (fetch failed or no externalPath), the section is omitted
    gracefully — the file is still written with metadata only.
    """
    safe_id = _sanitize_filename(job["job_id"])
    md_path = job_details_dir / f"job_{safe_id}.md"

    if md_path.exists():
        return  # Defensive guard — never overwrite

    title           = job.get("title", "N/A")
    company         = job.get("_company", "N/A")
    location        = job.get("_location", "N/A")
    employment_type = job.get("_employment_type", "N/A")
    url             = job.get("_url", "N/A")
    discovered      = job.get("first_discovered_on", datetime.date.today().isoformat())

    content = f"""# {title}

| Field            | Value |
|------------------|-------|
| **Company**      | {company} |
| **Location**     | {location} |
| **Type**         | {employment_type} |
| **Discovered**   | {discovered} |
| **URL**          | {url} |

---

## Notes

<!-- Add your personal notes, interview prep, and application tracking here -->

"""

    if description:
        content += f"""---

## Job Description

{description}
"""

    md_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Function 5 — Per-company orchestration wrapper
# ---------------------------------------------------------------------------

def process_company(
    company_cfg: dict,
    fresh_jobs:  list[dict],
    paths:       dict,
) -> dict[str, Any]:
    """
    Full state-tracking cycle for one employer.

    Detail fetching flow:
      1. reconcile() returns new_jobs with '_external_path' populated.
      2. A single stateful detail_session [NET-2.1] is created here and
         shared across all detail GETs for this company run.
      3. fetch_job_detail() is called ONLY for new_jobs entries — enforcing
         the safety guardrail that existing jobs never trigger a detail fetch.
      4. description (or None on failure) is passed to write_job_detail().

    Args:
        company_cfg : company dict from config.yaml (includes api_url)
        fresh_jobs  : normalized job list from workday_scraper.fetch_jobs()
        paths       : path dict from config_engine.resolve_output_paths()

    Returns:
        summary dict: {new, updated, delisted}
    """
    name:            str  = company_cfg["name"]
    api_url:         str  = company_cfg["api_url"]
    csv_path:        Path = paths["root_dir"] / "master_jobs.csv"
    job_details_dir: Path = paths["job_details_dir"]

    # 1. Load existing ledger
    ledger = load_ledger(csv_path)

    # 2. Reconcile — new_jobs list is explicitly populated inside reconcile()
    ledger, summary = reconcile(ledger, fresh_jobs)

    # 3. Persist CSV
    write_ledger(ledger, csv_path)

    # 4. Build a stateful detail session [NET-2.1] for all description GETs.
    #    Safety guardrail: this session is ONLY used for new_jobs — existing
    #    entries never enter the loop below.
    detail_session = requests.Session()
    detail_session.headers.update(workday_scraper._BASE_HEADERS)

    new_with_desc  = 0
    new_no_desc    = 0

    for new_job in summary["new_jobs"]:
        ext_path    = new_job.get("_external_path", "")
        description = None

        if ext_path:
            description = workday_scraper.fetch_job_detail(
                detail_session, api_url, ext_path
            )

        write_job_detail(new_job, job_details_dir, description)

        if description:
            new_with_desc += 1
        else:
            new_no_desc += 1

    # 5. Terminal status summary
    new_count      = summary["new"]
    updated_count  = summary["updated"]
    delisted_count = summary["delisted"]
    total_rows     = len(ledger)
    md_files       = len(list(job_details_dir.glob("job_*.md")))

    print(f"  [{name}] new={new_count}  updated={updated_count}  delisted={delisted_count}")
    if new_count > 0:
        print(f"  [{name}] Descriptions fetched: {new_with_desc} ok / {new_no_desc} skipped")
    print(f"  [{name}] CSV rows   : {total_rows}  → {csv_path}")
    print(f"  [{name}] Detail .md : {md_files}    → {job_details_dir}")

    return {"new": new_count, "updated": updated_count, "delisted": delisted_count}


# ---------------------------------------------------------------------------
# Standalone verification entrypoint  [EXEC-4.3]
# ---------------------------------------------------------------------------

def main() -> None:
    print()
    print("=== state_tracker.py — Task 3: State Tracking & File Writer (v2) ===")
    print()

    config    = config_engine.load_config("config.yaml")
    all_paths = config_engine.resolve_output_paths(config)
    path_map  = {p["name"]: p for p in all_paths}

    companies = config.get("companies") or []
    if not companies:
        print("[ERROR] No companies in config.yaml.")
        sys.exit(1)

    for company_cfg in companies:
        ats_type = company_cfg.get("ats_type", "").lower()
        name     = company_cfg.get("name", "unknown")

        if ats_type != "workday":
            print(f"  [SKIP] {name} — ats_type '{ats_type}' not yet supported")
            continue

        if name not in path_map:
            print(f"  [ERROR] No path entry for '{name}'. Check config.yaml.")
            sys.exit(1)

        print(f"  Fetching live jobs for: {name}")
        fresh_jobs = workday_scraper.fetch_jobs(company_cfg)

        print()
        process_company(company_cfg, fresh_jobs, path_map[name])

    print()
    print("[DONE] Task 3 complete.")
    print()


if __name__ == "__main__":
    main()
