"""
state_tracker.py — Task 3: State Tracking & File Writer Module (v3)
--------------------------------------------------------------------
Implements the deduplication routine against the master_jobs.csv ledger,
writes per-job Markdown detail files, and fetches rich metadata + full
job descriptions using a self-healing file check pattern.

Architectural design (v3 — user-approved under [EXEC-4.2]):
  - NEW job_id     → create job_<id>.md with full metadata + description
  - EXISTING id    → update last_date + visible=yes; if .md is missing or lacks
                     "## Job Description", self-heal by fetching and writing/appending
  - DELISTED id    → mark visible=no in CSV; leave .md file intact

Self-healing loop:
  Iterates fresh_jobs (full list). For each:
    • .md missing             → fetch detail, create new file
    • .md exists, no section  → fetch detail, APPEND section (preserve user notes)
    • .md complete            → skip (zero network calls on normal runs)

Late Write CSV pattern:
  write_ledger() executes LAST — after the detail loop updates
  ledger[job_id]["first_discovered_on"] with the authoritative HR startDate.

Rules cited:
  [EXEC-4.1]  Single task scope.
  [EXEC-4.3]  Done when terminal confirms CSV rows + .md files on disk.
  [NET-2.1]   Stateful requests.Session() for all detail GETs.
  [NET-2.3]   Non-200 on detail: warn + continue (does not abort batch).
  [SEC-3.2]   All writes go to ./targets/[company]/ paths only.
  [TECH-1.1]  Pure Python 3.14 stdlib + project-local imports.
  [TECH-1.4]  html2text via workday_scraper.fetch_job_detail().
"""

import csv
import datetime
import sys
import threading

# Reconfigure stdout/stderr encoding errors to prevent Windows UnicodeEncodeErrors on console print
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass

import logging
import builtins

from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
import json
import filelock

try:
    from . import config_engine       # when imported as part of the src package
    from . import workday_scraper     # when imported as part of the src package
    from . import custom_scrapers     # non-Workday ATS plugins (DIC, NISG)
except ImportError:
    import config_engine              # when run directly: python src/state_tracker.py
    import workday_scraper
    import custom_scrapers

# ── Log Management (Docker Safe) ──────────────────────────────────────────
def _setup_logger():
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    logger = logging.getLogger("state_tracker")
    logger.setLevel(logging.INFO)
    
    if not logger.handlers:
        fh = RotatingFileHandler(log_dir / "scraper.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
        fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
        
    return logger

_logger = _setup_logger()

_in_print = threading.local()

def _custom_print(*args, **kwargs):
    if getattr(_in_print, "active", False):
        try:
            sys.__stdout__.write(" ".join(str(a) for a in args) + "\n")
        except Exception:
            pass
        return
    _in_print.active = True
    try:
        msg = " ".join(str(a) for a in args)
        _logger.info(msg)
    except Exception:
        try:
            sys.__stdout__.write(" ".join(str(a) for a in args) + "\n")
        except Exception:
            pass
    finally:
        _in_print.active = False

# Override built-in print globally for this script
builtins.print = _custom_print




# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Function 1 — Load existing CSV ledger
# ---------------------------------------------------------------------------

def load_ledger(csv_path: Path) -> dict[str, dict]:
    """
    Read master_jobs.csv into a dict keyed by job_id.
    Returns empty dict on first run — absence is valid, not an error.
    """
    if not csv_path.exists():
        return {}

    ledger: dict[str, dict] = {}
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            job_id = row.get("job_id", "").strip()
            if job_id:
                d = dict(row)
                # Auto-migrate legacy 7-column CSVs that predate the 'skipped' column
                if "skipped" not in d:
                    d["skipped"] = ""
                ledger[job_id] = d
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

    new_jobs is explicitly populated inside this function and returned via
    the summary payload. Each entry carries all enrichment fields including
    '_external_path' required by fetch_job_detail().

    NOTE: The self-healing loop in process_company() operates over the full
    fresh_jobs list — not new_jobs — so detail fetching can repair existing
    incomplete files. new_jobs is retained for count reporting only.
    """
    today:     str       = datetime.date.today().isoformat()
    fresh_ids: set[str]  = {job["job_id"] for job in fresh_jobs}

    new_count:      int        = 0
    updated_count:  int        = 0
    delisted_count: int        = 0
    new_jobs:       list[dict] = []

    # Pass 1: process every job in the fresh pull
    for job in fresh_jobs:
        job_id = job["job_id"]

        if job_id not in ledger:
            ledger[job_id] = {
                "job_id":              job_id,
                "title":               job.get("title", ""),
                "first_discovered_on": job.get("first_discovered_on") or today,
                "visible":             "yes",
                "relevance":           job.get("relevance", "TBD"),
                "applied":             job.get("applied", "no"),
                "last_date":           job.get("last_date") or "",
                "skipped":             "",
                # Enrichment fields — stripped from CSV by extrasaction='ignore'
                "_company":           job.get("_company", ""),
                "_url":               job.get("_url", ""),
                "_location":          job.get("_location", ""),
                "_employment_type":   job.get("_employment_type", ""),
                "_external_path":     job.get("_external_path", ""),
            }
            new_count += 1
            new_jobs.append(ledger[job_id])
        else:
            # last_date is the HR application deadline — never overwrite with today
            ledger[job_id]["visible"] = "yes"
            if job.get("last_date"):
                ledger[job_id]["last_date"] = job["last_date"]
            # Update discovery date if the scraper has a real past date and ledger has today's date
            if job.get("first_discovered_on") and ledger[job_id].get("first_discovered_on") == today:
                ledger[job_id]["first_discovered_on"] = job["first_discovered_on"]
            updated_count += 1

    # Pass 2: delist entries absent from fresh pull
    for job_id, row in ledger.items():
        if job_id not in fresh_ids:
            if row.get("visible") != "no":
                row["visible"] = "no"
                delisted_count += 1

    return ledger, {
        "new":      new_count,
        "updated":  updated_count,
        "delisted": delisted_count,
        "new_jobs": new_jobs,
    }


# ---------------------------------------------------------------------------
# Function 3 — Write CSV ledger
# ---------------------------------------------------------------------------

def write_ledger(ledger: dict[str, dict], csv_path: Path) -> None:
    """
    Persist the reconciled ledger to master_jobs.csv.

    LATE WRITE: called at the very end of process_company() so that
    ledger rows updated with authoritative startDate values during the
    detail loop are captured before the file is written.

    Column order: job_id, title, first_discovered_on, last_date, visible,
                  relevance, applied. All '_'-prefixed enrichment keys are
                  silently dropped (extrasaction='ignore').
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)


    lock_path = str(csv_path) + ".lock"
    with filelock.FileLock(lock_path, timeout=30):
        with csv_path.open(mode="w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=config_engine.CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in ledger.values():
                writer.writerow(row)


# ---------------------------------------------------------------------------
# Function 4 — Write per-job Markdown detail file
# ---------------------------------------------------------------------------

def write_job_detail(
    job:             dict,
    job_details_dir: Path,
    detail_dict:     dict | None = None,
) -> None:
    """
    Create job_<id>.md inside targets/[company]/job_details/.

    Called only for brand-new files (create mode). The self-healing append
    path in process_company() handles existing files directly to preserve
    user notes above the description section.

    When detail_dict is provided, canonical values override the search-result
    placeholders for location and URL, and new rows for Posted Date and Age
    are added to the metadata table.
    """
    safe_id = config_engine.sanitize_filename(job["job_id"])
    md_path = job_details_dir / f"job_{safe_id}.md"

    if md_path.exists():
        return  # defensive guard — never overwrite in create mode

    # Baseline values from search-result normalization
    title           = job.get("title", "N/A")
    company         = job.get("_company", "N/A")
    location        = job.get("_location", "N/A")
    employment_type = job.get("_employment_type", "N/A")
    url             = job.get("_url", "N/A")
    discovered      = job.get("first_discovered_on", datetime.date.today().isoformat())

    # Override with authoritative detail values when available
    posted_date = ""
    posted_on   = ""
    end_date    = ""
    if detail_dict:
        if detail_dict.get("exact_location"):
            location = detail_dict["exact_location"]
        if detail_dict.get("canonical_url"):
            url = detail_dict["canonical_url"]
        posted_date = detail_dict.get("start_date", "")
        posted_on   = detail_dict.get("posted_on", "")
        end_date    = detail_dict.get("end_date", "")

    # Build metadata table rows — conditionally add Posted Date and Age
    posted_date_row = f"| **Posted Date**  | {posted_date} |\n" if posted_date else ""
    age_row         = f"| **Age**          | {posted_on} |\n"   if posted_on   else ""
    deadline_row    = (
        f"| **Deadline**     | {end_date} |\n" if end_date
        else "| **Deadline**     | Not specified |\n"
    )

    content = f"""# {title}

| Field            | Value |
|------------------|-------|
| **Company**      | {company} |
| **Location**     | {location} |
| **Type**         | {employment_type} |
{posted_date_row}{age_row}{deadline_row}| **Discovered**   | {discovered} |
| **URL**          | {url} |

---

## Notes

<!-- Add your personal notes, interview prep, and application tracking here -->

"""

    if detail_dict and detail_dict.get("description"):
        content += f"""---

## Job Description

{detail_dict['description']}
"""

    md_path.parent.mkdir(parents=True, exist_ok=True)

    lock_md = str(md_path) + ".lock"
    with filelock.FileLock(lock_md, timeout=30):
        md_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Function 5 — Per-company orchestration wrapper
# ---------------------------------------------------------------------------

def process_company(
    company_cfg:         dict,
    fresh_jobs:          list[dict],
    paths:               dict,
    inline_descriptions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Full state-tracking cycle for one employer (v3 architecture).

    Execution order:
      1. load_ledger()       — read existing CSV state
      2. reconcile()         — three-way dedup; NO CSV write yet
      3. detail_session      — single stateful session (Workday only) [NET-2.1]
      4. self-healing loop   — iterate fresh_jobs (full list):
           Workday jobs:
             • .md missing            → fetch_job_detail() HTTP call, create file
             • .md exists, no section → fetch_job_detail() HTTP call, append section
             • .md complete           → skip (zero network calls)
           Custom ATS jobs (dic, nisg):
             • .md missing            → use inline_descriptions dict, create file
             • .md exists, no section → use inline_descriptions dict, append section
             • .md complete           → skip
      5. write_ledger()      — LATE WRITE: CSV now contains authoritative dates
      6. print summary

    Safety guardrail:
      fetch_job_detail() is only called when the on-disk .md file is incomplete.
      Jobs with fully-populated .md files generate zero network requests.
    """
    name:            str  = company_cfg["name"]
    api_url:         str  = company_cfg.get("api_url", "")   # absent for dic/nisg
    csv_path:        Path = paths["root_dir"] / "master_jobs.csv"
    job_details_dir: Path = paths["job_details_dir"]

    # 1. Load existing ledger
    ledger = load_ledger(csv_path)

    # 2. Reconcile — in-memory only, no CSV write yet
    ledger, summary = reconcile(ledger, fresh_jobs)

    # 3. Stateful detail session (Workday only) [NET-2.1]
    detail_session = requests.Session()
    if api_url:
        detail_session.headers.update(workday_scraper._BASE_HEADERS)

    # Determine if this ATS provides inline descriptions (dic/nisg)
    is_custom_ats = inline_descriptions is not None

    fetched_ok = 0
    fetched_skip = 0
    healed = 0

    # 4. Self-healing loop — full fresh_jobs list
    for job in fresh_jobs:
        job_id   = job["job_id"]
        ext_path = job.get("_external_path", "")
        safe_id  = config_engine.sanitize_filename(job_id)
        md_path  = job_details_dir / f"job_{safe_id}.md"

        # Determine action based on on-disk state
        needs_fetch  = False
        append_mode  = False

        if not md_path.exists():
            needs_fetch = True
            append_mode = False   # brand-new file
        else:
            existing_text = md_path.read_text(encoding="utf-8")
            if "## Job Description" not in existing_text:
                needs_fetch = True
                append_mode = True    # file exists but description is missing
                healed += 1

        if not needs_fetch:
            continue

        detail_dict: dict | None = None

        if is_custom_ats:
            # Use pre-fetched inline description — no HTTP call needed
            inline_md = inline_descriptions.get(job_id, "")  # type: ignore[union-attr]
            if inline_md:
                detail_dict = {
                    "description":    inline_md,
                    "start_date":     "",
                    "end_date":       job.get("last_date", ""),
                    "posted_on":      "",
                    "exact_location": job.get("_location", ""),
                    "canonical_url":  job.get("_url", ""),
                }
                fetched_ok += 1
            else:
                fetched_skip += 1
        else:
            # Standard Workday detail fetch
            if ext_path:
                detail_dict = workday_scraper.fetch_job_detail(
                    detail_session, api_url, ext_path
                )

            # Late Write prep — update in-memory ledger with authoritative HR dates
            if detail_dict and job_id in ledger:
                if detail_dict.get("start_date"):
                    ledger[job_id]["first_discovered_on"] = detail_dict["start_date"]
                if detail_dict.get("end_date"):
                    ledger[job_id]["last_date"] = detail_dict["end_date"]

            if detail_dict:
                fetched_ok += 1
            else:
                fetched_skip += 1

        if append_mode:
            # Append description section to existing file — preserve user notes
            if detail_dict and detail_dict.get("description"):
                with md_path.open("a", encoding="utf-8") as fh:
                    fh.write(
                        f"\n---\n\n## Job Description\n\n"
                        f"{detail_dict['description']}\n"
                    )
        else:
            # Create new file with full metadata + description
            write_job_detail(job, job_details_dir, detail_dict)

    # 5. LATE WRITE — CSV now contains authoritative startDate values
    write_ledger(ledger, csv_path)

    # 6. Terminal summary
    new_count      = summary["new"]
    updated_count  = summary["updated"]
    delisted_count = summary["delisted"]
    total_rows     = len(ledger)
    md_files       = len(list(job_details_dir.glob("job_*.md")))

    print(f"  [{name}] new={new_count}  updated={updated_count}  delisted={delisted_count}  healed={healed}")
    print(f"  [{name}] Detail fetches : {fetched_ok} ok / {fetched_skip} skipped")
    print(f"  [{name}] CSV rows       : {total_rows}  -> {csv_path}")
    print(f"  [{name}] Detail .md     : {md_files}    -> {job_details_dir}")

    return {"new": new_count, "updated": updated_count, "delisted": delisted_count}


# ---------------------------------------------------------------------------
# Thread-safe per-company wrapper for parallel execution
# ---------------------------------------------------------------------------

def _scrape_one(company_cfg: dict, paths: dict) -> dict[str, Any]:
    """
    Scrape + process one company. Isolated for thread safety.
    Dispatches to the correct scraper based on ats_type.
    Outer try/except ensures one failing company never aborts the batch.
    """
    name     = company_cfg.get("name", "unknown")
    ats_type = company_cfg.get("ats_type", "workday").lower()
    print(f"  Fetching live jobs for: {name} (ats_type={ats_type})")

    try:
        if ats_type == "dic":
            fresh_jobs, inline_descriptions = custom_scrapers.fetch_jobs_dic(company_cfg)
        elif ats_type == "nisg":
            fresh_jobs, inline_descriptions = custom_scrapers.fetch_jobs_nisg(company_cfg)
        else:
            fresh_jobs = workday_scraper.fetch_jobs(company_cfg)
            inline_descriptions = None
    except Exception as exc:
        print(f"  [ERROR] {name} scraper raised an unexpected exception: {exc}")
        return {"new": 0, "updated": 0, "delisted": 0}

    print()
    return process_company(company_cfg, fresh_jobs, paths, inline_descriptions)



# ---------------------------------------------------------------------------
# Function 6 — Prune dead jobs (scorched-earth cleanup)
# ---------------------------------------------------------------------------

def prune_dead_jobs(config: dict, all_paths: list[dict]) -> dict[str, int]:
    """
    Permanently delete all artifacts for jobs that are no longer visible
    and have never been applied to. This includes skipped jobs.

    Criteria: visible == "no" AND applied == ""

    For each matching job:
      1. Delete job_<id>.md
      2. Delete job_<id>.scorecard.json
      3. Delete job_<id>.shortcomings.json
      4. Remove the row from master_jobs.csv

    Jobs with visible=="no" but applied != "" are retained (pending responses).
    """
    total_pruned = 0
    path_map = {p["name"]: p for p in all_paths}

    for company_cfg in (config.get("companies") or []):
        name = company_cfg.get("name", "")
        if name not in path_map:
            continue

        paths       = path_map[name]
        csv_path    = Path(paths["root_dir"]) / "master_jobs.csv"
        jd_dir      = Path(paths["job_details_dir"])
        sc_dir      = Path(paths["scorecards_dir"])
        sh_dir      = Path(paths["shortcomings_dir"])

        if not csv_path.exists():
            continue

        ledger    = load_ledger(csv_path)
        to_prune  = [
            job_id for job_id, row in ledger.items()
            if row.get("visible", "") == "no"
            and row.get("applied", "").strip().lower() in ("", "no")
        ]

        if not to_prune:
            continue

        pruned_count = 0
        for job_id in to_prune:
            safe_id = config_engine.sanitize_filename(job_id)

            for f in [
                jd_dir / f"job_{safe_id}.md",
                sc_dir / f"job_{safe_id}.scorecard.json",
                sh_dir / f"job_{safe_id}.shortcomings.json",
            ]:
                if f.exists():
                    f.unlink()

            del ledger[job_id]
            pruned_count += 1

        # Rewrite the CSV without the pruned rows
        write_ledger(ledger, csv_path)
        print(f"  [{name}] Pruned {pruned_count} dead job(s) from disk and ledger.")
        total_pruned += pruned_count

    return {"pruned": total_pruned}


# ---------------------------------------------------------------------------
# Standalone verification entrypoint  [EXEC-4.3]
# ---------------------------------------------------------------------------

def main() -> None:
    print()
    print("=== state_tracker.py — Task 3: State Tracking & File Writer (v3) ===")
    print()

    config    = config_engine.load_config("config.yaml")
    all_paths = config_engine.resolve_output_paths(config)
    path_map  = {p["name"]: p for p in all_paths}

    # ── --prune mode: scorched-earth cleanup of dead unapplied jobs ──────────
    if "--prune" in sys.argv:
        print("[PRUNE] Scanning for dead jobs (visible=no, applied='') ...")
        result = prune_dead_jobs(config, all_paths)
        total  = result["pruned"]
        print()
        if total:
            print(f"[DONE] Pruned {total} dead job(s) from disk and ledger(s).")
        else:
            print("[DONE] Nothing to prune — ledgers are clean.")
        print()
        sys.exit(0)

    companies = config.get("companies") or []
    if not companies:
        print("[ERROR] No companies in config.yaml.")
        sys.exit(1)

    max_workers = config.get("global_settings", {}).get("max_parallel_scrapers", 4)

    SUPPORTED_ATS = {"workday", "dic", "nisg"}

    supported_companies = []
    for company_cfg in companies:
        ats_type = company_cfg.get("ats_type", "").lower()
        name     = company_cfg.get("name", "unknown")

        if ats_type not in SUPPORTED_ATS:
            print(f"  [SKIP] {name} — ats_type '{ats_type}' not yet supported")
            continue

        if name not in path_map:
            print(f"  [ERROR] No path entry for '{name}'. Check config.yaml.")
            sys.exit(1)

        supported_companies.append(company_cfg)

    if not supported_companies:
        print("[WARN] No supported companies found.")
        sys.exit(0)

    if len(supported_companies) == 1:
        # Single company — run directly, no threading overhead
        _scrape_one(supported_companies[0], path_map[supported_companies[0]["name"]])
    else:
        # Multiple companies — run in parallel
        effective_workers = min(max_workers, len(supported_companies))
        print(f"  [PARALLEL] Scraping {len(supported_companies)} companies with {effective_workers} workers")
        print()

        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            futures = {
                executor.submit(_scrape_one, c, path_map[c["name"]]): c["name"]
                for c in supported_companies
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    print(f"  [ERROR] {name} failed: {exc}")

    print()
    
    # Save scrape metadata for UI

    base_dir = Path(config["global_settings"]["output_base_dir"])
    metadata_path = base_dir / "scrape_metadata.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps({
            "last_scrape": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }),
        encoding="utf-8"
    )
    
    print("[DONE] Task 3 complete.")
    print()


if __name__ == "__main__":
    main()
