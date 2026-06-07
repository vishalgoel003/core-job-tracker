import pandas as pd
from pathlib import Path
import sys

# Ensure we can import from src
sys.path.insert(0, str(Path(__file__).parent.parent))
from src import config_engine

def main():
    print("=== LLM State Cleanup Utility ===")
    
    # Load config dynamically so we respect output_base_dir
    try:
        config = config_engine.load_config("config.yaml")
        all_paths = config_engine.resolve_output_paths(config)
    except Exception as e:
        print(f"Error loading config: {e}")
        return

    total_scorecards = 0
    total_shortcomings = 0
    updated_csvs = 0

    # 1. Clean per-company directories based on actual configured paths
    for p in all_paths:
        # Delete scorecards
        scorecards_dir = p["scorecards_dir"]
        if scorecards_dir.exists():
            for f in scorecards_dir.glob("*.json"):
                f.unlink()
                total_scorecards += 1

        # Delete shortcomings
        shortcomings_dir = p["shortcomings_dir"]
        if shortcomings_dir.exists():
            for f in shortcomings_dir.glob("*.json"):
                f.unlink()
                total_shortcomings += 1

        # Reset Relevance in master_jobs.csv
        csv_path = p["root_dir"] / "master_jobs.csv"
        if csv_path.exists():
            try:
                df = pd.read_csv(csv_path, dtype=str)
                if "relevance" in df.columns:
                    df["relevance"] = "0"
                    df.to_csv(csv_path, index=False)
                    updated_csvs += 1
            except Exception as e:
                print(f"⚠️ Failed to process {csv_path}: {e}")

    print(f"✅ Deleted {total_scorecards} scorecard JSON files.")
    print(f"✅ Deleted {total_shortcomings} shortcomings JSON files.")
    print(f"✅ Reset 'relevance' to 0 in {updated_csvs} master_jobs.csv files.")

    # 3. Clear User Details insights artifacts
    profile_cfg = config.get("user_profile", {})
    resume_path = Path(profile_cfg.get("resume_path", "user_details/resume.md"))
    user_details_dir = resume_path.parent
    
    insights_files = [
        "digested_insights.json",
        "gap_fill_cache.json",
        "skill_gaps_ledger.jsonl",
        "skill_gaps_ledger_archive.jsonl"
    ]
    
    deleted_ud_files = 0
    if user_details_dir.exists():
        for filename in insights_files:
            file_path = user_details_dir / filename
            if file_path.exists():
                file_path.unlink()
                deleted_ud_files += 1
    print(f"✅ Deleted {deleted_ud_files} insight artifact files from {user_details_dir}")
    
    print("\nCleanup complete! You can now do manual testing.")
    print("Make sure to click 'Clear Cache' or restart the Streamlit app if it is currently open.")

if __name__ == "__main__":
    main()
