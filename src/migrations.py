"""
migrations.py — Run this once to backfill existing shortcomings into the new global ledger.
"""

import json
from pathlib import Path

def backfill_ledger():
    project_root = Path(__file__).parent.parent
    targets_dir = project_root / "targets"
    user_details_dir = project_root / "user_details"
    ledger_path = user_details_dir / "skill_gaps_ledger.jsonl"

    if not targets_dir.exists():
        print("No targets directory found.")
        return

    # Keep track of existing entries to prevent duplication if run twice
    existing_keys = set()
    if ledger_path.exists():
        with ledger_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    key = f"{entry.get('company')}_{entry.get('job_id')}"
                    existing_keys.add(key)
                except Exception:
                    continue

    migrated_count = 0
    skipped_count = 0

    user_details_dir.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as out_fh:
        # Walk through targets/*/shortcomings/*.shortcomings.json
        for company_dir in targets_dir.iterdir():
            if not company_dir.is_dir():
                continue
            
            company_name = company_dir.name
            shortcomings_dir = company_dir / "shortcomings"
            if not shortcomings_dir.exists():
                continue
            
            for file_path in shortcomings_dir.glob("*.shortcomings.json"):
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    job_id = data.get("job_id")
                    if not job_id:
                        continue
                    
                    key = f"{company_name}_{job_id}"
                    if key in existing_keys:
                        skipped_count += 1
                        continue

                    # Construct ledger entry
                    ledger_entry = {
                        "job_id": job_id,
                        "company": company_name,
                        "evaluated_at": data.get("evaluated_at"),
                        "resume_hash": data.get("resume_hash", ""),
                        "shortcomings": data.get("shortcomings", []),
                        "_meta": data.get("_meta", {}),
                    }
                    
                    out_fh.write(json.dumps(ledger_entry, ensure_ascii=False) + "\n")
                    existing_keys.add(key)
                    migrated_count += 1

                except Exception as e:
                    print(f"Error processing {file_path}: {e}")

    print(f"Migration complete! Migrated: {migrated_count}, Skipped (already in ledger): {skipped_count}")

if __name__ == "__main__":
    backfill_ledger()
