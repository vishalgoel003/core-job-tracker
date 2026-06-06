"""
config_engine.py — Task 1: Configuration Engine
------------------------------------------------
Parses config.yaml and dynamically generates the required output directory
tree under ./targets/ for each configured employer.

Usage (inside activated venv, from project root):
    python src/config_engine.py
"""

import sys
from pathlib import Path
import re
import yaml

_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')

def sanitize_filename(job_id: str) -> str:
    """Replace Windows-illegal filename characters with '_'."""
    return _ILLEGAL_FILENAME_CHARS.sub("_", job_id)


# ---------------------------------------------------------------------------
# Global Constants & Schemas
# ---------------------------------------------------------------------------

# The canonical CSV schema for master_jobs.csv across all files
CSV_COLUMNS: list[str] = [
    "job_id",
    "title",
    "first_discovered_on",
    "last_date",
    "visible",
    "relevance",
    "applied",
    "skipped",
]

# ---------------------------------------------------------------------------
# Step 1 — Load & validate config.yaml
# ---------------------------------------------------------------------------

def load_config(path: str = "config.yaml") -> dict:
    """
    Open and parse config.yaml from the project root.

    Raises:
        FileNotFoundError: if the file does not exist at the given path.
        yaml.YAMLError:    if the file is present but malformed.
        KeyError:          if required top-level keys are missing.
    """
    config_path = Path(path)

    if not config_path.exists():
        raise FileNotFoundError(
            f"[ERROR] config.yaml not found at: {config_path.resolve()}\n"
            f"Please copy 'config.yaml.sample' to 'config.yaml' and edit it with your settings before proceeding."
        )

    with config_path.open("r", encoding="utf-8") as fh:
        try:
            config = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise yaml.YAMLError(f"[ERROR] Failed to parse config.yaml — {exc}") from exc

    # Guard required top-level keys
    for required_key in ("global_settings", "companies"):
        if required_key not in config:
            raise KeyError(
                f"[ERROR] config.yaml is missing required top-level key: '{required_key}'"
            )

    if "output_base_dir" not in config["global_settings"]:
        raise KeyError(
            "[ERROR] config.yaml → global_settings is missing required key: 'output_base_dir'"
        )

    print(f"[OK]  config.yaml loaded — "
          f"{len(config['companies'])} company/companies found.")
    return config


# ---------------------------------------------------------------------------
# Step 2 — Resolve per-employer output paths
# ---------------------------------------------------------------------------

def resolve_output_paths(config: dict) -> list[dict]:
    """
    Build the set of required directory paths for every employer defined
    in config.yaml.

    Returns a list of dicts, one per company:
        {
            "name":            str,
            "ats_type":        str,
            "root_dir":        Path,   # targets/<company_name>/
            "job_details_dir": Path,   # targets/<company_name>/job_details/
        }
    """
    base_dir = Path(config["global_settings"]["output_base_dir"])
    results = []

    for company in config["companies"]:
        name     = company["name"]
        ats_type = company.get("ats_type", "unknown")

        root_dir          = base_dir / name
        job_details_dir   = root_dir / "job_details"
        scorecards_dir    = root_dir / "scorecards"
        shortcomings_dir  = root_dir / "shortcomings"

        results.append(
            {
                "name":              name,
                "ats_type":          ats_type,
                "root_dir":          root_dir,
                "job_details_dir":   job_details_dir,
                "scorecards_dir":    scorecards_dir,
                "shortcomings_dir":  shortcomings_dir,
            }
        )

    return results


# ---------------------------------------------------------------------------
# Step 3 — Create directories on disk
# ---------------------------------------------------------------------------

def bootstrap_directories(paths: list[dict]) -> None:
    """
    Idempotently create the full directory tree for every employer.
    Prints a creation-status line for each directory so terminal output
    is unambiguous during verification.
    """
    print()
    print("=" * 60)
    print("  Bootstrapping target directory tree")
    print("=" * 60)

    for entry in paths:
        name             = entry["name"]
        job_details_dir  = entry["job_details_dir"]
        scorecards_dir   = entry["scorecards_dir"]
        shortcomings_dir = entry["shortcomings_dir"]

        # mkdir(parents=True) creates the employer root AND subdirs together
        already_existed = job_details_dir.exists()
        job_details_dir.mkdir(parents=True, exist_ok=True)
        scorecards_dir.mkdir(parents=True, exist_ok=True)
        shortcomings_dir.mkdir(parents=True, exist_ok=True)

        status = "[EXISTS]" if already_existed else "[CREATED]"

        print()
        print(f"  Employer  : {name}  (ATS: {entry['ats_type']})")
        print(f"  Root dir  : {entry['root_dir'].resolve()}")
        print(f"  Details   : {job_details_dir.resolve()}  {status}")
        print(f"  Scorecards: {scorecards_dir.resolve()}")
        print(f"  Shortcmgs : {shortcomings_dir.resolve()}")

    print()
    print("=" * 60)
    print("  Directory bootstrap complete.")
    print("=" * 60)
    print()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    print()
    print("=== config_engine.py — Task 1: Configuration Engine ===")
    print(f"    Working directory : {Path.cwd()}")
    print(f"    Python executable : {sys.executable}")
    print()

    config = load_config("config.yaml")
    paths  = resolve_output_paths(config)
    bootstrap_directories(paths)

    print("[DONE] Task 1 verification complete — inspect ./targets/ to confirm.")
    print()


if __name__ == "__main__":
    main()
