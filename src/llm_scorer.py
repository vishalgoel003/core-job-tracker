"""
llm_scorer.py — LLM Scoring Pipeline (Scorecard Generation + Resume Evaluation)
---------------------------------------------------------------------------------
Two-pass architecture:
  Pass 1: Job Description → LLM → Scorecard JSON    (what does this job need?)
  Pass 2: Resume + Scorecard → LLM → Score + Gaps   (how well do I match?)
  Pass 3: Shortcomings + LinkedIn → LLM → Gap Report (can I cover the gaps?)

CLI-first design — all functions work standalone. The Streamlit UI wraps them.

Usage:
    python src/llm_scorer.py                                    # Score all unscored jobs
    python src/llm_scorer.py --job JR-0000105811 --company Barclays  # Score one job
    python src/llm_scorer.py --gap-check JR-0000105811 --company Barclays  # LinkedIn gap check

AGENT.md compliance:
  [TECH-1.5]  LLM calls via requests. No SDK dependencies.
  [NET-2.4]   Rate-limit aware via llm_client cascade.
  [SEC-3.2]   All writes go to ./targets/[company]/ paths only.
  [SEC-3.3]   API keys loaded from config.yaml at runtime.
  [EXEC-4.4]  On-demand only. Never auto-triggered during scraping.
"""

import argparse
import csv
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any

import filelock

try:
    from . import config_engine
    from . import llm_client
except ImportError:
    import config_engine
    import llm_client


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOCK_TIMEOUT_S = 5

CSV_COLUMNS: list[str] = [
    "job_id",
    "title",
    "first_discovered_on",
    "last_date",
    "visible",
    "relevance",
    "applied",
]


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

SCORECARD_SYSTEM_PROMPT = """You are a hiring analyst. Given a job description, extract a structured evaluation scheme as JSON.

Your output JSON MUST follow this exact structure. The example below shows the required depth and detail — your output MUST match this level:

{
  "meta": {
    "role": "<exact job title>",
    "domains": ["<domain1>", "<domain2>"],
    "seniority_level": "<Junior|Mid|Senior|Lead|Principal|Manager|Director>"
  },
  "hard_filters": {
    "min_total_yoe": <integer or null>,
    "regulatory_compliance": ["<standard1>"] or [],
    "specific_credential": ["<degree/cert required>"] or []
  },
  "pillars": {
    "<PILLAR_CODE>": {
      "suggested_weight": <integer>,
      "req": ["<specific required skill or technology>", "<another required skill>"],
      "equiv": ["<acceptable alternative from JD>"] or []
    }
  }
}

=== EXAMPLE OUTPUT (for a different job — use as a structural template only) ===
{
  "meta": {
    "role": "Lead Software Engineer",
    "domains": ["Payments", "Enterprise Software"],
    "seniority_level": "Lead"
  },
  "hard_filters": {
    "min_total_yoe": null,
    "regulatory_compliance": [],
    "specific_credential": []
  },
  "pillars": {
    "TECH": {
      "suggested_weight": 30,
      "req": ["Core Java", "Spring Boot", "SQL", "RDBMS", "NoSQL"],
      "equiv": ["C# .NET Core", "Entity Framework", "DynamoDB"]
    },
    "SYS": {
      "suggested_weight": 20,
      "req": ["Microservices Architecture", "Distributed Systems", "Design Patterns"],
      "equiv": ["SOA Design", "Event-Driven Architecture"]
    },
    "OPS": {
      "suggested_weight": 15,
      "req": ["Kafka", "Cloud Deployment", "Maven/Gradle", "GIT"],
      "equiv": ["RabbitMQ", "AWS ECS", "CI/CD Pipelines"]
    },
    "SEC": {
      "suggested_weight": 15,
      "req": ["Spring Security", "Authentication Concepts", "Authorization"],
      "equiv": ["OAuth2", "OIDC", "OWASP Standards"]
    },
    "DOM": {
      "suggested_weight": 10,
      "req": ["Payments Domain", "Agile Methodologies"],
      "equiv": ["Fintech", "Scrum"]
    },
    "LDR": {
      "suggested_weight": 5,
      "req": ["Technical Leadership", "Code Reviews", "Mentoring"],
      "equiv": ["Team Lead", "Peer Review Coordination"]
    },
    "MSC": {
      "suggested_weight": 5,
      "req": ["AI Augmented Development", "Intellectual Property Protection"],
      "equiv": ["GitHub Copilot", "DevSecOps AI Tools"]
    }
  }
}
=== END EXAMPLE ===

Rules:
1. Pillar codes are short uppercase abbreviations (3-4 chars): TECH, SYS, OPS, SEC, DOM, LDR, MSC, DATA, QA, etc. Create as many pillars as the job requires — typically 5-8 for a real job description.
2. Use relative integer weights for suggested_weight (e.g., 30, 20, 15). They do NOT need to sum to any specific value — they will be normalized automatically.
3. CRITICAL: "req" must list SPECIFIC technologies, frameworks, tools, or skills named in the JD — NOT generic categories. For example, write "Core Java" and "Spring Boot" instead of "Programming Languages". Write "Kafka" and "Zookeeper" instead of "Messaging". Write "OAuth2" instead of "Security Concepts". Be as granular as the JD text allows.
4. Every pillar MUST have at least one item in "req". Empty req arrays are not allowed.
5. "equiv" lists acceptable alternatives explicitly mentioned in the JD (e.g., "Oracle or PostgreSQL", "AWS or Azure or GCP"). Leave empty [] if no alternatives are mentioned.
6. Return ONLY valid JSON. No explanations, no markdown fences, no preamble."""

EVALUATION_SYSTEM_PROMPT = """You are a resume evaluator. Given a candidate resume and a job scorecard (JSON), evaluate how well the resume matches the job requirements.

Your output JSON must have this exact structure:
{
  "relevance": <integer 0-100>,
  "shortcomings": [
    "<specific gap or missing requirement 1>",
    "<specific gap or missing requirement 2>"
  ]
}

Rules:
1. "relevance" is a weighted score from 0-100 based on the pillar weights in the scorecard.
2. Consider both "req" and "equiv" skills — if the candidate has an equivalent, count it as partial match.
3. "shortcomings" must list ONLY specific, actionable gaps — not vague observations.
4. If the candidate exceeds a requirement, note it positively in your scoring but don't list it as a shortcoming.
5. Consider hard_filters: if the candidate clearly fails a hard filter (e.g., min YoE), reduce score significantly.
6. Return ONLY valid JSON. No explanations, no markdown fences, no preamble."""

GAP_ANALYSIS_SYSTEM_PROMPT = """You are a career data analyst. Given a list of shortcomings from a job evaluation and the candidate's LinkedIn profile data, determine which gaps can be covered by information in the LinkedIn data.

Your output JSON must have this exact structure:
{
  "coverable": [
    {
      "gap": "<the shortcoming text>",
      "evidence": "<specific data from LinkedIn that covers this gap>",
      "source_file": "<which CSV file contained the evidence>"
    }
  ],
  "uncoverable": [
    "<shortcoming that has no evidence in LinkedIn data>"
  ]
}

Rules:
1. Only list a gap as "coverable" if there is CONCRETE evidence in the LinkedIn data.
2. Quote specific entries, project names, skill names, or position descriptions as evidence.
3. If a gap is partially coverable, include it in "coverable" with honest evidence.
4. Return ONLY valid JSON. No explanations, no markdown fences, no preamble."""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _strip_notes_section(md_text: str) -> str:
    """
    Strip the ## Notes section (and everything after) from a job .md file
    before sending to LLM. This ensures user notes and old shortcomings
    never contaminate the LLM prompt.
    """
    # Find ## Notes and strip from there
    notes_idx = md_text.find("## Notes")
    if notes_idx >= 0:
        # Also strip the --- before Notes if present
        before = md_text[:notes_idx].rstrip()
        if before.endswith("---"):
            before = before[:-3].rstrip()
        return before
    return md_text


def _normalize_weights(scorecard: dict) -> dict:
    """
    Normalize pillar weights so they sum to exactly 1000.
    Modifies the scorecard in-place and returns it.
    """
    pillars = scorecard.get("pillars")
    if not pillars or not isinstance(pillars, dict):
        return scorecard

    total = sum(p.get("suggested_weight", 0) for p in pillars.values())
    if total <= 0:
        # Distribute equally
        equal_weight = 1000 // len(pillars)
        for p in pillars.values():
            p["suggested_weight"] = equal_weight
        # Fix rounding — give remainder to first pillar
        remainder = 1000 - (equal_weight * len(pillars))
        if remainder > 0:
            first_key = next(iter(pillars))
            pillars[first_key]["suggested_weight"] += remainder
        return scorecard

    if total == 1000:
        return scorecard

    # Scale proportionally
    scale = 1000.0 / total
    running_total = 0
    pillar_keys = list(pillars.keys())

    for i, key in enumerate(pillar_keys):
        if i == len(pillar_keys) - 1:
            # Last pillar gets the remainder to ensure exact 1000
            pillars[key]["suggested_weight"] = 1000 - running_total
        else:
            new_weight = round(pillars[key]["suggested_weight"] * scale)
            pillars[key]["suggested_weight"] = new_weight
            running_total += new_weight

    return scorecard


def _read_linkedin_data(config: dict) -> str:
    """
    Read and concatenate relevant LinkedIn CSV files into a single text block.
    Each file's content is prefixed with its filename for source attribution.
    """
    profile_cfg = config.get("user_profile") or {}
    linkedin_dir = Path(profile_cfg.get("linkedin_dir", "user_details/Basic_LinkedInDataExport"))
    linkedin_files = profile_cfg.get("linkedin_files") or []

    parts: list[str] = []
    for filename in linkedin_files:
        filepath = linkedin_dir / filename
        if filepath.exists():
            try:
                content = filepath.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(f"=== {filename} ===\n{content}")
            except Exception:
                continue

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Pass 1: Scorecard Generation
# ---------------------------------------------------------------------------

def generate_scorecard(
    jd_text: str,
    providers: list[llm_client.ProviderConfig],
    stage_params: llm_client.StageParams | None = None,
) -> dict | None:
    """
    Send job description to LLM and extract a structured evaluation scorecard.

    Post-processing:
      - Normalizes pillar weights to sum to exactly 1000
      - Validates JSON structure

    Returns the scorecard dict, or None on failure.
    """
    params = stage_params or llm_client.StageParams(temperature=0.15, max_tokens=1500)

    raw_text, used_provider = llm_client.call_llm(
        providers=providers,
        system_prompt=SCORECARD_SYSTEM_PROMPT,
        user_prompt=jd_text,
        stage="scorecard",
        stage_params=params,
        json_mode=True,
    )

    if not raw_text:
        print("  [SCORER] Scorecard generation failed — no LLM response.")
        return None

    scorecard = llm_client.extract_json(raw_text)
    if not scorecard:
        print("  [SCORER] Failed to parse scorecard JSON from LLM response.")
        print(f"           Raw text (first 300 chars): {raw_text[:300]}")
        return None

    # Validate essential structure
    if "pillars" not in scorecard:
        print("  [SCORER] Scorecard missing 'pillars' key. Invalid response.")
        return None

    # Post-validation: warn if scorecard appears collapsed
    pillars = scorecard.get("pillars", {})
    if len(pillars) < 3:
        print(f"  [SCORER] WARNING: Scorecard has only {len(pillars)} pillar(s) — possible schema collapse. Consider re-generating.")
    else:
        empty_req = [k for k, v in pillars.items() if not v.get("req")]
        if empty_req:
            print(f"  [SCORER] WARNING: Pillar(s) {empty_req} have empty 'req' arrays — possible schema collapse.")

    # Normalize weights to sum to 1000 (models output relative weights, we scale)
    scorecard = _normalize_weights(scorecard)

    return scorecard


# ---------------------------------------------------------------------------
# Pass 2: Resume Evaluation
# ---------------------------------------------------------------------------

def evaluate_resume(
    resume_md: str,
    scorecard: dict,
    providers: list[llm_client.ProviderConfig],
    stage_params: llm_client.StageParams | None = None,
) -> dict | None:
    """
    Send resume + scorecard to LLM and get a relevance score + shortcomings.

    Post-processing:
      - Clamps relevance to 0–100
      - Ensures shortcomings is a list of strings

    Returns {"relevance": int, "shortcomings": list[str]}, or None on failure.
    """
    params = stage_params or llm_client.StageParams(temperature=0.10, max_tokens=800)

    user_prompt = (
        "## Candidate Resume\n\n"
        f"{resume_md}\n\n"
        "---\n\n"
        "## Job Scorecard\n\n"
        f"```json\n{json.dumps(scorecard, indent=2)}\n```"
    )

    raw_text, used_provider = llm_client.call_llm(
        providers=providers,
        system_prompt=EVALUATION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        stage="evaluation",
        stage_params=params,
        json_mode=True,
    )

    if not raw_text:
        print("  [SCORER] Resume evaluation failed — no LLM response.")
        return None

    result = llm_client.extract_json(raw_text)
    if not result:
        print("  [SCORER] Failed to parse evaluation JSON from LLM response.")
        return None

    # Post-process: clamp relevance
    relevance = result.get("relevance", 0)
    try:
        relevance = max(0, min(100, int(relevance)))
    except (TypeError, ValueError):
        relevance = 0
    result["relevance"] = relevance

    # Post-process: ensure shortcomings is list of strings
    shortcomings = result.get("shortcomings", [])
    if not isinstance(shortcomings, list):
        shortcomings = [str(shortcomings)] if shortcomings else []
    result["shortcomings"] = [str(s) for s in shortcomings]

    return result


# ---------------------------------------------------------------------------
# Pass 3: LinkedIn Gap Analysis
# ---------------------------------------------------------------------------

def check_linkedin_gaps(
    shortcomings: list[str],
    linkedin_data: str,
    providers: list[llm_client.ProviderConfig],
    stage_params: llm_client.StageParams | None = None,
) -> dict | None:
    """
    Send shortcomings + LinkedIn data to LLM to identify coverable gaps.

    Returns {"coverable": [...], "uncoverable": [...]}, or None on failure.
    """
    if not shortcomings:
        return {"coverable": [], "uncoverable": []}

    if not linkedin_data.strip():
        return {"coverable": [], "uncoverable": shortcomings}

    params = stage_params or llm_client.StageParams(temperature=0.10, max_tokens=1200)

    user_prompt = (
        "## Shortcomings to Check\n\n"
        + "\n".join(f"- {s}" for s in shortcomings)
        + "\n\n---\n\n"
        "## LinkedIn Profile Data\n\n"
        f"{linkedin_data}"
    )

    raw_text, used_provider = llm_client.call_llm(
        providers=providers,
        system_prompt=GAP_ANALYSIS_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        stage="gap_analysis",
        stage_params=params,
        json_mode=True,
    )

    if not raw_text:
        print("  [SCORER] Gap analysis failed — no LLM response.")
        return None

    result = llm_client.extract_json(raw_text)
    if not result:
        print("  [SCORER] Failed to parse gap analysis JSON from LLM response.")
        return None

    # Ensure expected keys exist
    if "coverable" not in result:
        result["coverable"] = []
    if "uncoverable" not in result:
        result["uncoverable"] = []

    return result


# ---------------------------------------------------------------------------
# Orchestrator: score_job
# ---------------------------------------------------------------------------

def score_job(
    company_name: str,
    job_id: str,
    config: dict,
    scorecard_override: dict | None = None,
) -> dict:
    """
    Full scoring pipeline for one job:
      1. Read JD .md from disk (strip ## Notes before LLM call)
      2. Load or generate scorecard
      3. Evaluate resume against scorecard
      4. Save scorecard + shortcomings to disk
      5. Update relevance in master_jobs.csv via filelock
      6. Return full result dict

    Args:
        company_name: Company directory name under targets/
        job_id: The job_id from CSV
        config: Full parsed config.yaml dict
        scorecard_override: If provided, use this instead of generating/loading
    """
    # Resolve paths
    all_paths = config_engine.resolve_output_paths(config)
    path_map = {p["name"]: p for p in all_paths}

    if company_name not in path_map:
        return {"error": f"Company '{company_name}' not found in config."}

    paths = path_map[company_name]
    job_details_dir  = Path(paths["job_details_dir"])
    scorecards_dir   = Path(paths["scorecards_dir"])
    shortcomings_dir = Path(paths["shortcomings_dir"])
    csv_path         = Path(paths["root_dir"]) / "master_jobs.csv"

    safe_id = config_engine.sanitize_filename(job_id)

    # 1. Read JD
    md_path = job_details_dir / f"job_{safe_id}.md"
    if not md_path.exists():
        return {"error": f"Job detail file not found: {md_path}"}

    md_text = md_path.read_text(encoding="utf-8")
    jd_text = _strip_notes_section(md_text)

    if not jd_text.strip():
        return {"error": "Job description is empty after stripping notes."}

    # Load LLM config
    providers, stage_params_map = llm_client.load_llm_config(config)
    if not providers:
        return {"error": "No LLM providers configured. Add api_key to config.yaml."}

    # 2. Load or generate scorecard
    scorecard_path = scorecards_dir / f"job_{safe_id}.scorecard.json"

    if scorecard_override:
        scorecard = scorecard_override
        print(f"  [SCORER] Using provided scorecard override for {job_id}")
    elif scorecard_path.exists():
        try:
            scorecard = json.loads(scorecard_path.read_text(encoding="utf-8"))
            print(f"  [SCORER] Loaded existing scorecard for {job_id}")
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [SCORER] Failed to load scorecard, regenerating: {e}")
            scorecard = None
    else:
        scorecard = None

    if scorecard is None:
        print(f"  [SCORER] Generating scorecard for {job_id} ...")
        scorecard = generate_scorecard(
            jd_text,
            providers,
            stage_params=stage_params_map.get("scorecard"),
        )
        if not scorecard:
            return {"error": "Scorecard generation failed."}

    # Save scorecard
    scorecards_dir.mkdir(parents=True, exist_ok=True)
    scorecard_path.write_text(
        json.dumps(scorecard, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  [SCORER] Scorecard saved → {scorecard_path}")

    # 3. Read resume
    profile_cfg = config.get("user_profile") or {}
    resume_path = Path(profile_cfg.get("resume_path", "user_details/resume.md"))
    if not resume_path.exists():
        return {"error": f"Resume not found: {resume_path}", "scorecard": scorecard}

    resume_md = resume_path.read_text(encoding="utf-8")

    # 4. Evaluate resume
    print(f"  [SCORER] Evaluating resume against scorecard ...")
    evaluation = evaluate_resume(
        resume_md,
        scorecard,
        providers,
        stage_params=stage_params_map.get("evaluation"),
    )

    if not evaluation:
        return {"error": "Resume evaluation failed.", "scorecard": scorecard}

    relevance = evaluation["relevance"]
    shortcomings = evaluation["shortcomings"]

    # 5. Save shortcomings
    shortcomings_dir.mkdir(parents=True, exist_ok=True)
    shortcomings_path = shortcomings_dir / f"job_{safe_id}.shortcomings.json"
    shortcomings_data = {
        "job_id": job_id,
        "company": company_name,
        "relevance": relevance,
        "evaluated_at": datetime.datetime.now().isoformat(),
        "resume_version": str(resume_path),
        "shortcomings": shortcomings,
    }
    shortcomings_path.write_text(
        json.dumps(shortcomings_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  [SCORER] Shortcomings saved → {shortcomings_path}")

    # 6. Update CSV relevance via filelock
    if csv_path.exists():
        lock_path = str(csv_path) + ".lock"
        try:
            with filelock.FileLock(lock_path, timeout=_LOCK_TIMEOUT_S):
                rows: list[dict] = []
                with csv_path.open(newline="", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
                        if row.get("job_id", "").strip() == job_id:
                            row["relevance"] = str(relevance)
                        rows.append(row)

                with csv_path.open("w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(rows)

                print(f"  [SCORER] CSV updated: {job_id} → relevance={relevance}")
        except filelock.Timeout:
            print(f"  [SCORER] WARNING: Could not acquire lock on {csv_path.name}. CSV not updated.")

    return {
        "job_id": job_id,
        "company": company_name,
        "relevance": relevance,
        "shortcomings": shortcomings,
        "scorecard": scorecard,
        "scorecard_path": str(scorecard_path),
        "shortcomings_path": str(shortcomings_path),
    }


# ---------------------------------------------------------------------------
# Batch mode: score all unscored jobs
# ---------------------------------------------------------------------------

def score_all_unscored(
    company_name: str | None,
    config: dict,
) -> list[dict]:
    """
    Find all jobs with relevance=0 and score them sequentially.
    If company_name is None, process all companies.
    """
    all_paths = config_engine.resolve_output_paths(config)
    llm_cfg = config.get("llm") or {}
    delay = float(llm_cfg.get("inter_call_delay_s", 2.0))

    results: list[dict] = []

    for paths in all_paths:
        name = paths["name"]
        if company_name and name != company_name:
            continue

        csv_path = Path(paths["root_dir"]) / "master_jobs.csv"
        if not csv_path.exists():
            continue

        # Find unscored jobs
        unscored: list[str] = []
        with csv_path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    rel = int(row.get("relevance", 0))
                except (TypeError, ValueError):
                    rel = 0
                if rel == 0 and row.get("visible", "").lower() == "yes":
                    unscored.append(row["job_id"])

        if not unscored:
            print(f"  [{name}] No unscored visible jobs.")
            continue

        print(f"  [{name}] {len(unscored)} unscored job(s) to process.")

        import time
        for i, job_id in enumerate(unscored):
            print(f"\n  [{name}] Scoring {i+1}/{len(unscored)}: {job_id}")
            result = score_job(name, job_id, config)
            results.append(result)

            if "error" in result:
                print(f"  [{name}] {job_id}: {result['error']}")
            else:
                print(f"  [{name}] {job_id}: relevance={result['relevance']}")

            # Polite delay between LLM calls
            if i < len(unscored) - 1:
                time.sleep(delay)

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="LLM Scoring Pipeline — Scorecard Generation + Resume Evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python src/llm_scorer.py                                          # Score all unscored jobs\n"
            "  python src/llm_scorer.py --job JR-0000105811 --company Barclays    # Score one job\n"
            "  python src/llm_scorer.py --gap-check JR-0000105811 --company Barclays  # LinkedIn gap check\n"
        ),
    )
    parser.add_argument("--job", type=str, help="Score a specific job by ID")
    parser.add_argument("--company", type=str, help="Company name (required with --job)")
    parser.add_argument("--gap-check", type=str, metavar="JOB_ID",
                        help="Run LinkedIn gap analysis for a specific job")
    parser.add_argument("--company-filter", type=str,
                        help="Filter to a specific company in batch mode")

    args = parser.parse_args()

    print()
    print("=== llm_scorer.py — LLM Scoring Pipeline ===")
    print()

    config = config_engine.load_config("config.yaml")

    if args.gap_check:
        # LinkedIn gap analysis mode
        if not args.company:
            print("[ERROR] --company is required with --gap-check")
            sys.exit(1)

        # Load shortcomings from disk
        all_paths = config_engine.resolve_output_paths(config)
        path_map = {p["name"]: p for p in all_paths}
        if args.company not in path_map:
            print(f"[ERROR] Company '{args.company}' not found in config.")
            sys.exit(1)

        safe_id = config_engine.sanitize_filename(args.gap_check)
        shortcomings_path = Path(path_map[args.company]["shortcomings_dir"]) / f"job_{safe_id}.shortcomings.json"

        if not shortcomings_path.exists():
            print(f"[ERROR] No shortcomings file found. Score the job first:")
            print(f"  python src/llm_scorer.py --job {args.gap_check} --company {args.company}")
            sys.exit(1)

        shortcomings_data = json.loads(shortcomings_path.read_text(encoding="utf-8"))
        shortcomings = shortcomings_data.get("shortcomings", [])

        if not shortcomings:
            print("[INFO] No shortcomings to check — resume is a perfect match!")
            sys.exit(0)

        linkedin_data = _read_linkedin_data(config)
        providers, stage_params_map = llm_client.load_llm_config(config)

        result = check_linkedin_gaps(
            shortcomings, linkedin_data, providers,
            stage_params=stage_params_map.get("gap_analysis"),
        )

        if result:
            print(f"\n{'='*60}")
            print(f"  Gap Analysis for {args.gap_check} ({args.company})")
            print(f"{'='*60}")
            if result.get("coverable"):
                print(f"\n  ✅ Coverable gaps ({len(result['coverable'])}):")
                for item in result["coverable"]:
                    print(f"    • {item['gap']}")
                    print(f"      Evidence: {item['evidence']}")
                    print(f"      Source: {item['source_file']}")
            if result.get("uncoverable"):
                print(f"\n  ❌ Uncoverable gaps ({len(result['uncoverable'])}):")
                for gap in result["uncoverable"]:
                    print(f"    • {gap}")
        else:
            print("[FAIL] Gap analysis returned no results.")

    elif args.job:
        # Single job scoring mode
        if not args.company:
            print("[ERROR] --company is required with --job")
            sys.exit(1)

        result = score_job(args.company, args.job, config)

        if "error" in result:
            print(f"\n[ERROR] {result['error']}")
            sys.exit(1)

        print(f"\n{'='*60}")
        print(f"  Scoring Result: {args.job} ({args.company})")
        print(f"{'='*60}")
        print(f"  Relevance Score: {result['relevance']}/100")
        if result.get("shortcomings"):
            print(f"\n  Shortcomings ({len(result['shortcomings'])}):")
            for s in result["shortcomings"]:
                print(f"    • {s}")
        print(f"\n  Scorecard: {result.get('scorecard_path')}")
        print(f"  Shortcomings: {result.get('shortcomings_path')}")

    else:
        # Batch mode — score all unscored
        results = score_all_unscored(args.company_filter, config)
        scored = [r for r in results if "error" not in r]
        failed = [r for r in results if "error" in r]

        print(f"\n{'='*60}")
        print(f"  Batch Scoring Complete")
        print(f"{'='*60}")
        print(f"  Scored: {len(scored)}  |  Failed: {len(failed)}")
        if scored:
            avg = sum(r["relevance"] for r in scored) / len(scored)
            print(f"  Average relevance: {avg:.1f}/100")

    print()
    print("[DONE] LLM scoring pipeline complete.")
    print()


if __name__ == "__main__":
    main()
