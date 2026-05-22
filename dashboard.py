"""
dashboard.py — Task 4: Aggregated Operational Dashboard
--------------------------------------------------------
Read-only standalone script that reads master_jobs.csv across all employer
directories and prints a rich chronological operational overview.

Zero network calls. Zero file writes. Pure terminal output.

Rules cited:
  [EXEC-4.1]  Single task scope — dashboard.py only.
  [SEC-3.2]   Read-only access to ./targets/[company]/ paths only.
  [TECH-1.1]  Pure Python 3.14 stdlib: csv, pathlib, datetime, os, argparse.

Usage:
    python dashboard.py                     # full chronological dashboard
    python dashboard.py --deadlines         # deadline alerts only
    python dashboard.py --company Barclays  # filter to one company
    python dashboard.py --days 7            # deadline window = 7 days
"""

import argparse
import csv
import datetime
import sys
from pathlib import Path
from typing import Any

import config_engine

# ---------------------------------------------------------------------------
# ANSI colour palette — degrades gracefully on non-colour terminals
# ---------------------------------------------------------------------------

def _supports_colour() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

_COL    = _supports_colour()
RESET   = "\033[0m"   if _COL else ""
BOLD    = "\033[1m"   if _COL else ""
DIM     = "\033[2m"   if _COL else ""
CYAN    = "\033[96m"  if _COL else ""
GREEN   = "\033[92m"  if _COL else ""
YELLOW  = "\033[93m"  if _COL else ""
RED     = "\033[91m"  if _COL else ""
MAGENTA = "\033[95m"  if _COL else ""
WHITE   = "\033[97m"  if _COL else ""

# Column widths for the chronological job table
_W_DATE  = 12
_W_ID    = 16
_W_TITLE = 46
_W_VIS   =  7
_W_APP   =  7
_W_DL    = 12


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_company_data(
    name:            str,
    csv_path:        Path,
    job_details_dir: Path,
) -> dict[str, Any] | None:
    """
    Read master_jobs.csv for one employer.
    Returns a data dict, or None if the CSV does not exist yet.
    """
    if not csv_path.exists():
        return None

    rows: list[dict] = []
    with csv_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows.append(row)

    md_count = len(list(job_details_dir.glob("job_*.md"))) if job_details_dir.exists() else 0
    csv_mtime = datetime.datetime.fromtimestamp(csv_path.stat().st_mtime)

    return {
        "name":      name,
        "rows":      rows,
        "md_count":  md_count,
        "csv_path":  csv_path,
        "csv_mtime": csv_mtime,
    }


# ---------------------------------------------------------------------------
# Summary computation
# ---------------------------------------------------------------------------

def compute_summary(rows: list[dict]) -> dict[str, Any]:
    """Compute aggregate statistics from a list of CSV row dicts."""
    total    = len(rows)
    active   = sum(1 for r in rows if r.get("visible", "").lower() == "yes")
    delisted = sum(1 for r in rows if r.get("visible", "").lower() == "no")
    applied  = sum(1 for r in rows if r.get("applied", "").lower() == "yes")
    with_dl  = sum(1 for r in rows if r.get("last_date", "").strip())

    dates = sorted(
        [r.get("first_discovered_on", "") for r in rows if r.get("first_discovered_on")],
        reverse=True,
    )
    newest_date = dates[0] if dates else ""
    oldest_date = dates[-1] if dates else ""

    newest_title = next(
        (r.get("title", "—") for r in rows if r.get("first_discovered_on") == newest_date), "—"
    )
    oldest_title = next(
        (r.get("title", "—") for r in rows if r.get("first_discovered_on") == oldest_date), "—"
    )

    span_days = 0
    if newest_date and oldest_date and newest_date != oldest_date:
        try:
            span_days = (
                datetime.date.fromisoformat(newest_date)
                - datetime.date.fromisoformat(oldest_date)
            ).days
        except ValueError:
            pass

    return {
        "total":         total,
        "active":        active,
        "delisted":      delisted,
        "applied":       applied,
        "with_deadline": with_dl,
        "newest_date":   newest_date,
        "oldest_date":   oldest_date,
        "newest_title":  newest_title,
        "oldest_title":  oldest_title,
        "span_days":     span_days,
    }


# ---------------------------------------------------------------------------
# Print helpers
# ---------------------------------------------------------------------------

def _hr(char: str = "─", width: int = 70) -> str:
    return char * width


def print_banner(company_count: int) -> None:
    today = datetime.date.today().isoformat()
    print()
    print(f"{BOLD}{CYAN}{_hr('═')}{RESET}")
    print(f"{BOLD}{WHITE}  CORE JOB TRACKER — Operational Dashboard{RESET}")
    print(f"{DIM}  Generated : {today}  │  Companies tracked : {company_count}{RESET}")
    print(f"{BOLD}{CYAN}{_hr('═')}{RESET}")


def print_company_block(name: str, summary: dict, md_count: int) -> None:
    print()
    bar_len = max(60 - len(name) - 1, 4)
    print(f"{BOLD}{MAGENTA}── {name} {_hr('─', bar_len)}{RESET}")
    print()

    vis_str = f"{GREEN}{summary['active']}{RESET}"
    del_str = f"{RED}{summary['delisted']}{RESET}" if summary["delisted"] else f"{DIM}0{RESET}"
    app_str = f"{YELLOW}{summary['applied']}{RESET}" if summary["applied"] else f"{DIM}0{RESET}"

    print(f"  {BOLD}Total tracked  :{RESET} {summary['total']}")
    print(f"  {BOLD}Active         :{RESET} {vis_str}")
    print(f"  {BOLD}Delisted       :{RESET} {del_str}")
    print(f"  {BOLD}Applied        :{RESET} {app_str}")
    print(f"  {BOLD}With deadline  :{RESET} {summary['with_deadline']} / {summary['total']}")
    print(f"  {BOLD}Detail files   :{RESET} {md_count} .md")
    print()
    if summary["newest_date"]:
        print(
            f"  {BOLD}Newest posting :{RESET} {CYAN}{summary['newest_date']}{RESET}"
            f"  {summary['newest_title'][:50]}"
        )
    if summary["oldest_date"]:
        print(
            f"  {BOLD}Oldest posting :{RESET} {DIM}{summary['oldest_date']}{RESET}"
            f"  {summary['oldest_title'][:50]}"
        )
    if summary["span_days"]:
        print(f"  {BOLD}Posting span   :{RESET} {summary['span_days']} days on the board")


def _visible_cell(val: str) -> str:
    v = val.lower().strip()
    if v == "yes":
        return f"{GREEN}{'yes':^{_W_VIS}}{RESET}"
    if v == "no":
        return f"{RED}{'no':^{_W_VIS}}{RESET}"
    return f"{DIM}{'?':^{_W_VIS}}{RESET}"


def _applied_cell(val: str) -> str:
    if val.lower().strip() == "yes":
        return f"{YELLOW}{'yes':^{_W_APP}}{RESET}"
    return f"{DIM}{'no':^{_W_APP}}{RESET}"


def _deadline_cell(val: str) -> str:
    v = val.strip()
    if not v:
        return f"{DIM}{'—':^{_W_DL}}{RESET}"
    today = datetime.date.today()
    try:
        dl        = datetime.date.fromisoformat(v)
        days_left = (dl - today).days
        if days_left <= 0:
            return f"{RED}{v[:_W_DL]:<{_W_DL}}{RESET}"
        if days_left <= 14:
            return f"{YELLOW}{v[:_W_DL]:<{_W_DL}}{RESET}"
    except ValueError:
        pass
    return f"{v[:_W_DL]:<{_W_DL}}"


def print_job_table(rows: list[dict]) -> None:
    """Print all jobs sorted by first_discovered_on descending (newest first)."""
    if not rows:
        print(f"  {DIM}No jobs found.{RESET}")
        return

    sorted_rows = sorted(
        rows,
        key=lambda r: r.get("first_discovered_on") or "",
        reverse=True,
    )

    print()
    print(
        f"  {BOLD}"
        f"{'Posted':<{_W_DATE}}  "
        f"{'Job ID':<{_W_ID}}  "
        f"{'Title':<{_W_TITLE}}  "
        f"{'Vis':^{_W_VIS}}  "
        f"{'Applied':^{_W_APP}}  "
        f"{'Deadline':<{_W_DL}}"
        f"{RESET}"
    )
    print(
        f"  {DIM}"
        f"{_hr('-', _W_DATE)}  "
        f"{_hr('-', _W_ID)}  "
        f"{_hr('-', _W_TITLE)}  "
        f"{_hr('-', _W_VIS)}  "
        f"{_hr('-', _W_APP)}  "
        f"{_hr('-', _W_DL)}"
        f"{RESET}"
    )

    for r in sorted_rows:
        date     = (r.get("first_discovered_on") or "")[:_W_DATE]
        job_id   = (r.get("job_id") or "")[:_W_ID]
        title    = (r.get("title") or "")[:_W_TITLE]
        visible  = _visible_cell(r.get("visible", ""))
        applied  = _applied_cell(r.get("applied", ""))
        deadline = _deadline_cell(r.get("last_date", ""))

        print(
            f"  {CYAN}{date:<{_W_DATE}}{RESET}  "
            f"{job_id:<{_W_ID}}  "
            f"{title:<{_W_TITLE}}  "
            f"{visible}  "
            f"{applied}  "
            f"{deadline}"
        )


def print_deadline_alerts(rows: list[dict], name: str, days: int = 14) -> None:
    """Print jobs whose last_date closes within `days` days."""
    today  = datetime.date.today()
    alerts = []

    for r in rows:
        dl_raw = r.get("last_date", "").strip()
        if not dl_raw:
            continue
        try:
            dl        = datetime.date.fromisoformat(dl_raw)
            days_left = (dl - today).days
            if 0 <= days_left <= days:
                alerts.append((days_left, dl_raw, r.get("job_id", ""), r.get("title", "")))
        except ValueError:
            continue

    if not alerts:
        return

    alerts.sort()
    print()
    print(f"  {BOLD}{YELLOW}⚠  DEADLINES — {name} (closing within {days} days):{RESET}")
    for days_left, dl_raw, job_id, title in alerts:
        urgency = (
            f"{RED}TODAY   {RESET}" if days_left == 0
            else f"{YELLOW}{days_left}d left{RESET}"
        )
        print(f"  {urgency}  {job_id:<16}  {title[:50]}  — closes {dl_raw}")


def print_footer(company_data_list: list[dict]) -> None:
    total_jobs = sum(len(d["rows"]) for d in company_data_list)
    total_md   = sum(d["md_count"] for d in company_data_list)
    print()
    print(f"{BOLD}{CYAN}{_hr('─')}{RESET}")
    print(f"  {DIM}Total jobs tracked : {total_jobs}  │  Detail files : {total_md} .md{RESET}")
    for d in company_data_list:
        mtime = d["csv_mtime"].strftime("%Y-%m-%d %H:%M")
        print(f"  {DIM}{d['name']} ledger last updated : {mtime}{RESET}")
    print(f"{BOLD}{CYAN}{_hr('─')}{RESET}")
    print()


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Core Job Tracker — Aggregated Operational Dashboard",
    )
    parser.add_argument(
        "--deadlines", action="store_true",
        help="Show only deadline alerts (jobs closing within --days days)",
    )
    parser.add_argument(
        "--company", metavar="NAME",
        help="Filter dashboard to a single company by name",
    )
    parser.add_argument(
        "--days", type=int, default=14, metavar="N",
        help="Deadline alert window in days (default: 14)",
    )
    args = parser.parse_args()

    config    = config_engine.load_config("config.yaml")
    all_paths = config_engine.resolve_output_paths(config)
    path_map  = {p["name"]: p for p in all_paths}

    companies = config.get("companies") or []
    if not companies:
        print("[ERROR] No companies in config.yaml.")
        sys.exit(1)

    if args.company:
        companies = [c for c in companies if c["name"].lower() == args.company.lower()]
        if not companies:
            print(f"[ERROR] No company named '{args.company}' in config.yaml.")
            sys.exit(1)

    company_data_list: list[dict] = []
    for company_cfg in companies:
        name = company_cfg["name"]
        if name not in path_map:
            print(f"  [WARN] No path entry for '{name}' — skipping.")
            continue
        csv_path        = path_map[name]["root_dir"] / "master_jobs.csv"
        job_details_dir = path_map[name]["job_details_dir"]
        data = load_company_data(name, csv_path, job_details_dir)
        if data is None:
            print(f"  [WARN] No CSV for '{name}' — run state_tracker.py first.")
            continue
        company_data_list.append(data)

    if not company_data_list:
        print("[ERROR] No data loaded. Run state_tracker.py first.")
        sys.exit(1)

    if args.deadlines:
        print()
        print(f"{BOLD}{YELLOW}=== Deadline Alerts (within {args.days} days) ==={RESET}")
        for data in company_data_list:
            print_deadline_alerts(data["rows"], data["name"], args.days)
        print()
        return

    print_banner(len(company_data_list))
    for data in company_data_list:
        summary = compute_summary(data["rows"])
        print_company_block(data["name"], summary, data["md_count"])
        print_deadline_alerts(data["rows"], data["name"], args.days)
        print_job_table(data["rows"])
    print_footer(company_data_list)


if __name__ == "__main__":
    main()
