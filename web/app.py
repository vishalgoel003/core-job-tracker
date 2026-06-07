"""
web/app.py — Job Application OS (v2)
--------------------------------------
Streamlit-based CRM front-end for the core-job-tracker pipeline.

Run from the project root:
    streamlit run web/app.py

Changes in v2:
  - "📖" action column in every tab triggers @st.dialog (replaces on_select + selectbox).
  - Editor version counter forces checkbox reset after modal open.
  - Tab 2 (Sent) is now st.data_editor — unchecking "App" returns job to Active Radar.
  - Condensed column headers to prevent horizontal scroll.
  - "🚀 Run Scraper" button in sidebar (subprocess.run → src/state_tracker.py).
  - Streamlit chrome hidden via CSS injection.
  - Imports updated to src.config_engine package path.
"""

import subprocess
import sys
import os
import json
import hashlib
from pathlib import Path

import datetime
import re
import time
import csv
import uuid
from collections import defaultdict

import filelock
import pandas as pd
import streamlit as st

# Path injection — allows `import src.config_engine` from the project root
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import src.config_engine as config_engine  # noqa: E402
import src.llm_client as llm_client        # noqa: E402
import src.llm_scorer as llm_scorer        # noqa: E402

# ---------------------------------------------------------------------------
# Page configuration  (must be the FIRST Streamlit call)
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Job Application OS",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# CSS — hide Streamlit chrome + table polish
# ---------------------------------------------------------------------------

st.markdown("""
<style>
    /* ── Hide Streamlit chrome (keep header so sidebar toggle remains accessible) ── */
    #MainMenu                    { visibility: hidden; }
    footer                       { visibility: hidden; }
    [data-testid="stToolbar"]    { display: none; }
    .block-container             { padding-top: 1.5rem !important; }

    /* ── Metric cards ── */
    [data-testid="stMetric"] {
        background    : #1a1a2e;
        border        : 1px solid #2d2d44;
        border-radius : 8px;
        padding       : 4px 8px !important; /* Tighter padding */
    }
    [data-testid="stMetricValue"] { font-size: 1.2rem !important; }

    /* ── Tab labels ── */
    .stTabs [data-baseweb="tab"] {
        font-size   : 14px;
        font-weight : 600;
        padding     : 8px 18px;
    }

    /* ── Data editor ── */
    [data-testid="stDataEditor"] { border-radius: 8px; }

    /* ── Disable native table sorting ── */
    [data-testid="stDataEditor"] th {
        pointer-events: none !important;
    }

    /* ── Sidebar accent ── */
    [data-testid="stSidebar"] h2 { color: #a78bfa; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOCK_TIMEOUT_S = 5

SORT_OPTIONS = ["Posted Date ↓", "Deadline ↑", "Relevance ↓", "Company A→Z"]
_SORT_MAP = {
    "Posted Date ↓": ("first_discovered_on",   False),
    "Deadline ↑":    ("last_date",             True),
    "Relevance ↓":   ("relevance",             False),
    "Company A→Z":   ("_company",              True),
}

# ---------------------------------------------------------------------------
# Tab configurations (DRY refactor)
# ---------------------------------------------------------------------------

_COMMON_COLS   = ["view_url", "_company", "job_id", "title", "first_discovered_on", "last_date"]
_ACTIVE_BASE   = ["selected", "relevance"] + _COMMON_COLS
_SENT_BASE     = ["selected", "relevance", "applied"] + _COMMON_COLS
_ARCHIVED_BASE = _COMMON_COLS.copy()
_SKIPPED_BASE  = ["selected"] + _COMMON_COLS

# ---------------------------------------------------------------------------
# Markdown notes helpers
# ---------------------------------------------------------------------------

_NOTES_RE = re.compile(r"(## Notes\n\n)(.*?)(?=\n\n---|$)", re.DOTALL)


def _read_notes(md_text: str) -> str:
    m = _NOTES_RE.search(md_text)
    if not m:
        return ""
    body = m.group(2)
    return "" if body.startswith("<!--") else body


def _write_notes(md_text: str, new_notes: str) -> str:
    def _rep(m: re.Match) -> str:
        return f"## Notes\n\n{new_notes}"
    result = _NOTES_RE.sub(_rep, md_text, count=1)
    if result == md_text:
        result = md_text.rstrip() + f"\n\n## Notes\n\n{new_notes}\n"
    return result


# ---------------------------------------------------------------------------
# Applied field helpers (backward-compatible with legacy "yes"/"no" values)
# ---------------------------------------------------------------------------

def _not_applied(val: str) -> bool:
    return val.strip().lower() in ("", "no")


def _is_applied(val: str) -> bool:
    return not _not_applied(val)


# ---------------------------------------------------------------------------
# Skipped field helpers
# ---------------------------------------------------------------------------

def _is_skipped(val: str) -> bool:
    return str(val).strip().lower() == "yes"


def _not_skipped(val: str) -> bool:
    return not _is_skipped(val)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30)
def load_all_jobs() -> tuple[pd.DataFrame, float]:
    """
    Concat master_jobs.csv from every configured company.
    Injects _company, _csv_path, _md_dir per-row (never displayed).
    TTL=30s keeps the cache fresh without a manual refresh.
    Returns (DataFrame, unix_timestamp_when_loaded).
    """
    config    = config_engine.load_config("config.yaml")
    all_paths = config_engine.resolve_output_paths(config)

    frames: list[pd.DataFrame] = []
    for p in all_paths:
        csv_path        = Path(p["root_dir"]) / "master_jobs.csv"
        job_details_dir = Path(p["job_details_dir"])
        if not csv_path.exists():
            continue
        try:
            df = pd.read_csv(csv_path, dtype=str).fillna("")
        except pd.errors.EmptyDataError:
            continue
        df["_company"]  = p["name"]
        df["_csv_path"] = str(csv_path)
        df["_md_dir"]   = str(job_details_dir)
        frames.append(df)

    if not frames:

        return pd.DataFrame(), time.time()

    combined = pd.concat(frames, ignore_index=True)
    combined["relevance"] = (
        pd.to_numeric(combined["relevance"], errors="coerce").fillna(0).astype(int)
    )
    combined["first_discovered_on"] = pd.to_datetime(
        combined["first_discovered_on"], errors="coerce"
    )
    combined["last_date"] = pd.to_datetime(combined["last_date"], errors="coerce")
    combined["applied_bool"] = combined["applied"].apply(_is_applied)
    # Auto-add skipped column for CSVs that predate the feature
    if "skipped" not in combined.columns:
        combined["skipped"] = ""
    combined["skipped_bool"] = combined["skipped"].astype(str).str.lower().isin(["yes", "true", "1"])
    if "visible" not in combined.columns:
        combined["visible"] = "yes"
    combined["visible_bool"] = combined["visible"].astype(str).str.lower().isin(["yes", "true", "1"])
    combined["view_url"] = combined.apply(
        lambda r: f"/?company={r['_company']}&job_id={r['job_id']}", axis=1
    )

    # Memory optimization: Downcast low-cardinality columns
    for col in ["_company", "ats_type"]:
        if col in combined.columns:
            combined[col] = combined[col].astype("category")


    return combined, time.time()


# ---------------------------------------------------------------------------
# Write-back (filelock)
# ---------------------------------------------------------------------------

def _write_back(changes: list[dict]) -> None:
    """
    Write field changes to per-company CSVs using filelock.
    Groups changes by CSV path to minimise file opens.
    On timeout: shows warning (scraper may be writing), does not crash.
    On success: clears cache + reruns.
    """
    by_csv: dict[str, list] = defaultdict(list)
    for c in changes:
        by_csv[c["csv_path"]].append(c)

    for csv_str, group in by_csv.items():
        csv_path  = Path(csv_str)
        lock_path = csv_str + ".lock"
        try:
            with filelock.FileLock(lock_path, timeout=_LOCK_TIMEOUT_S):
                raw = pd.read_csv(csv_path, dtype=str)
                for c in group:
                    raw.loc[raw["job_id"] == c["job_id"], c["field"]] = str(c["value"])
                raw.to_csv(csv_path, index=False)
        except filelock.Timeout:
            st.warning(
                f"⚠️ Could not acquire write lock on `{csv_path.name}` — "
                "the background scraper may be running. Try again in a moment."
            )
            continue

    st.cache_data.clear()
    st.rerun()


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------

def _apply_sort(df: pd.DataFrame, sort1: str, sort2: str) -> pd.DataFrame:
    col1, asc1 = _SORT_MAP[sort1]
    col2, asc2 = _SORT_MAP[sort2]
    return (
        df.sort_values(by=[col1, col2], ascending=[asc1, asc2], na_position="last")
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# Column config
# ---------------------------------------------------------------------------

_COL_CFG = {
    "view_url":            st.column_config.LinkColumn(
                               "🔗",
                               help="Open job details in a new tab",
                               width="small",
                               display_text="View",
                           ),
    "selected":            st.column_config.CheckboxColumn(
                               "☑️",
                               help="Select rows for bulk actions",
                               width="small",
                               default=False,
                           ),
    "relevance":           st.column_config.NumberColumn(
                               "⭐", min_value=0, max_value=100, step=1, width="small",
                           ),
    "_company":            st.column_config.TextColumn("Company",  disabled=True, width="small"),
    "job_id":              st.column_config.TextColumn("ID",       disabled=True, width="small"),
    "title":               st.column_config.TextColumn("Title",    disabled=True),
    "first_discovered_on": st.column_config.DateColumn("Added",    disabled=True, width="small"),
    "last_date":           st.column_config.DateColumn("Due",      disabled=True, width="small"),
    "applied":             st.column_config.TextColumn("App Date", disabled=True, width="small"),
}


# ---------------------------------------------------------------------------
# Job detail page  (standalone — served at /?company=X&job_id=Y)
# ---------------------------------------------------------------------------

def render_job_detail_page(company: str, job_id: str) -> None:
    """
    Full-page job detail view routed via URL query params.
    Opens in a new browser tab; safe behind Cloudflare Tunnel (relative URLs).
    Never calls load_all_jobs() — reads only the files for this one job.
    """
    config    = config_engine.load_config("config.yaml")
    all_paths = config_engine.resolve_output_paths(config)
    path_map  = {p["name"]: p for p in all_paths}

    if company not in path_map:
        st.error(f"❌ Company **{company}** not found in config.yaml.")
        st.stop()

    paths    = path_map[company]
    safe_id  = config_engine.sanitize_filename(job_id)
    md_dir   = Path(paths["job_details_dir"])
    sc_dir   = Path(paths["scorecards_dir"])
    sh_dir   = Path(paths["shortcomings_dir"])
    md_path  = md_dir  / f"job_{safe_id}.md"
    sc_path  = sc_dir  / f"job_{safe_id}.scorecard.json"
    sh_path  = sh_dir  / f"job_{safe_id}.shortcomings.json"
    gap_path = sh_dir  / f"job_{safe_id}.gap_analysis.json"

    # ── Header ───────────────────────────────────────────────────────────────
    col_nav, col_title = st.columns([1, 5])
    with col_nav:
        st.markdown("[← Tracker](/)", unsafe_allow_html=True)
        if st.button("🔄 Refresh", key="detail_refresh"):
            st.rerun()
    with col_title:
        # Try to pull title from markdown h1 line
        md_text_raw = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        title_line  = next((line.lstrip("# ").strip() for line in md_text_raw.splitlines() if line.startswith("#")), job_id)
        st.markdown(f"### {title_line}")
        st.caption(f"**{company}**  ·  `{job_id}`")
    st.divider()

    # ── Resume hash (used for staleness checks) ───────────────────────────────
    profile_cfg = config.get("user_profile") or {}
    resume_path = Path(profile_cfg.get("resume_path", "user_details/resume.md"))
    current_resume_hash = ""
    if resume_path.exists():
        current_resume_hash = hashlib.sha256(
            resume_path.read_text(encoding="utf-8").encode()
        ).hexdigest()[:12]

    md_text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""

    # ── Load LLM config once ──────────────────────────────────────────────────
    providers, stage_params_map = llm_client.load_llm_config(config)
    has_providers = len(providers) > 0

    # ── Sub-tabs ─────────────────────────────────────────────────────────────
    tab_jd, tab_notes, tab_llm = st.tabs([
        "📄 Job Description",
        "✏️  My Notes",
        "🤖 LLM / Resume",
    ])

    # ════════════════════════════════════════════════════════════════
    # TAB: Job Description
    # ════════════════════════════════════════════════════════════════
    with tab_jd:
        if md_text:
            st.markdown(md_text, unsafe_allow_html=False)
        else:
            st.warning(
                "No detail file found.  \n"
                "Run `python src/state_tracker.py` to generate `.md` files."
            )

    # ════════════════════════════════════════════════════════════════
    # TAB: My Notes
    # ════════════════════════════════════════════════════════════════
    with tab_notes:
        file_mtime = md_path.stat().st_mtime if md_path.exists() else 0
        notes_key  = f"notes_input_{job_id}_{file_mtime}"

        current_notes = _read_notes(md_text)
        st.text_area(
            "Your notes — saved directly to the `.md` file",
            value=current_notes,
            height=340,
            key=notes_key,
            placeholder=(
                "Interview prep notes...\n"
                "Recruiter contact: ...\n"
                "Salary range: ...\n"
                "Key skills to highlight: ..."
            ),
        )

        def _handle_notes_save() -> None:
            if md_path.exists():
                current_md = md_path.read_text(encoding="utf-8")
                new_val    = st.session_state.get(notes_key, "")
                lock_md = str(md_path) + ".lock"

                with filelock.FileLock(lock_md, timeout=30):
                    md_path.write_text(_write_notes(current_md, new_val), encoding="utf-8")

        if st.button("💾 Save Notes", type="primary", width="stretch",
                     on_click=_handle_notes_save, key=f"save_notes_{job_id}"):
            st.success("✅ Notes saved to disk.")

    # ════════════════════════════════════════════════════════════════
    # TAB: LLM / Resume
    # ════════════════════════════════════════════════════════════════
    with tab_llm:
        if not has_providers:
            st.warning(
                "⚠️ **No LLM providers configured.**  \n"
                "Add at least one API key to `config.yaml → llm.providers` to enable scoring."
            )

        # ── Load persisted data from disk ───────────────────────────
        scorecard_data    = None
        shortcomings_data = None
        gap_data          = None

        if sc_path.exists():
            try:
                scorecard_data = json.loads(sc_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                scorecard_data = None

        if sh_path.exists():
            try:
                shortcomings_data = json.loads(sh_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                shortcomings_data = None

        if gap_path.exists():
            try:
                gap_data = json.loads(gap_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                gap_data = None

        # ── Helper: save gap analysis to disk ───────────────────────
        def _save_gap(gap_result: dict, supplementary_hash: str = "") -> None:
            gap_dir  = gap_path.parent
            gap_dir.mkdir(parents=True, exist_ok=True)
            gap_payload = {
                "job_id":      job_id,
                "company":     company,
                "analyzed_at": datetime.date.today().isoformat(),
                "resume_hash": current_resume_hash,
                "supplementary_hash": supplementary_hash,
                "coverable":   gap_result.get("coverable", []),
                "uncoverable": gap_result.get("uncoverable", []),
                "_meta":       gap_result.get("_meta", {})
            }
            lock_p = str(gap_path) + ".lock"
            with filelock.FileLock(lock_p, timeout=30):
                gap_path.write_text(
                    json.dumps(gap_payload, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

        # ════════════════════════════════════════════════════════
        # ⚡ EXPRESS PIPELINE (top-of-tab CTA)
        # ════════════════════════════════════════════════════════
        st.markdown("#### ⚡ Express Pipeline")
        st.caption("Runs all three stages sequentially: Scorecard → Resume Score → Supplementary Gap Analysis")
        if has_providers:
            if st.button("⚡ Run Express Pipeline", type="primary", key=f"express_{job_id}",
                         use_container_width=True):
                _express_result: dict = {}

                with st.status("Running Express Pipeline...", expanded=True) as status:
                    # Fetch supplementary data & hash for stage 3
                    supplementary_data = llm_scorer._read_supplementary_data(config)
                    current_supplementary_hash = hashlib.sha256(supplementary_data.encode()).hexdigest()[:12] if supplementary_data else ""

                    # Stage 1 — Scorecard (skip if exists)
                    st.write("📋 Stage 1: Scorecard…")
                    if not scorecard_data:
                        jd_clean = llm_scorer._strip_notes_section(md_text)
                        if jd_clean.strip():
                            sc = llm_scorer.generate_scorecard(
                                jd_clean, providers,
                                stage_params=stage_params_map.get("scorecard"),
                            )
                            if sc:
                                sc_dir.mkdir(parents=True, exist_ok=True)
                                lock_p = str(sc_path) + ".lock"
                
                                with filelock.FileLock(lock_p, timeout=30):
                                    sc_path.write_text(
                                        json.dumps(sc, indent=2, ensure_ascii=False),
                                        encoding="utf-8",
                                    )
                                scorecard_data = sc
                                st.write("  ✅ Scorecard generated.")
                            else:
                                st.write("  ❌ Scorecard generation failed.")
                        else:
                            st.write("  ⚠️ Job description is empty — skipped.")
                    else:
                        st.write("  ✅ Scorecard already exists — skipped.")

                    # Stage 2 — Resume evaluation
                    st.write("📊 Stage 2: Resume Evaluation…")
                    if scorecard_data:
                        if shortcomings_data and shortcomings_data.get("resume_hash") == current_resume_hash:
                            st.write("  ✅ Resume evaluation already exists and is up-to-date — skipped.")
                            _express_result = shortcomings_data
                        else:
                            eval_result = llm_scorer.score_job(
                                company, job_id, config,
                                scorecard_override=scorecard_data,
                            )
                            if "error" not in eval_result:
                                _express_result = eval_result
                                st.write(f"  ✅ Score: {eval_result['relevance']}/100")
                            else:
                                st.write(f"  ❌ {eval_result['error']}")
                    else:
                        st.write("  ⚠️ No scorecard — skipped.")

                    # Stage 3 — Gap analysis
                    st.write("🔍 Stage 3: Supplementary Gap Analysis…")
                    if gap_data and gap_data.get("resume_hash") == current_resume_hash and gap_data.get("supplementary_hash") == current_supplementary_hash:
                        st.write("  ✅ Gap analysis already exists and is up-to-date — skipped.")
                    else:
                        gaps_to_check = _express_result.get("shortcomings", [])
                        if gaps_to_check:
                            gap_result = llm_scorer.check_supplementary_gaps(
                                gaps_to_check, supplementary_data, providers,
                                stage_params=stage_params_map.get("gap_analysis"),
                            )
                            if gap_result:
                                _save_gap(gap_result, current_supplementary_hash)
                                st.write("  ✅ Gap analysis saved.")
                            else:
                                st.write("  ❌ Gap analysis failed.")
                        elif _express_result.get("relevance"):
                            st.write("  ✅ No shortcomings to check — strong match!")
                        else:
                            st.write("  ⚠️ No shortcomings available — skipped.")

                    status.update(label="Express Pipeline complete!", state="complete")

                st.cache_data.clear()
                st.rerun()

        st.divider()

        # ════════════════════════════════════════════════════════
        # SECTION A: Scorecard
        # ════════════════════════════════════════════════════════
        st.markdown("#### 📋 Job Scorecard")

        if scorecard_data:
            meta = scorecard_data.get("meta", {})
            st.caption(
                f"**Role:** {meta.get('role', 'N/A')}  ·  "
                f"**Domains:** {', '.join(meta.get('domains', []))}  ·  "
                f"**Level:** {meta.get('seniority_level', 'N/A')}"
            )

            hard = scorecard_data.get("hard_filters", {})
            if any(hard.values()):
                with st.expander("🚧 Hard Filters", expanded=False):
                    if hard.get("min_total_yoe"):
                        st.write(f"• Min YoE: **{hard['min_total_yoe']}**")
                    if hard.get("specific_credential"):
                        st.write(f"• Credentials: {', '.join(hard['specific_credential'])}")
                    if hard.get("regulatory_compliance"):
                        st.write(f"• Compliance: {', '.join(hard['regulatory_compliance'])}")

            pillars = scorecard_data.get("pillars", {})
            if pillars:
                st.dataframe(
                    pd.DataFrame([
                        {
                            "Pillar":     pname,
                            "Weight":     pdata.get("suggested_weight", 0),
                            "Required":   ", ".join(pdata.get("req", [])),
                            "Equivalents":  ", ".join(pdata.get("equiv", [])),
                        }
                        for pname, pdata in pillars.items()
                    ]),
                    hide_index=True,
                    use_container_width=True,
                )
            if "_meta" in scorecard_data and scorecard_data["_meta"]:
                meta = scorecard_data["_meta"]
                st.caption(f"*(Scorecard generated by **{meta.get('model')}** via {meta.get('provider')})*")

            with st.expander("✏️ Edit Scorecard JSON", expanded=False):
                edited_json = st.text_area(
                    "Raw JSON — edit and save",
                    value=json.dumps(scorecard_data, indent=2),
                    height=300,
                    key=f"sc_editor_{job_id}",
                )
                if st.button("💾 Save Edited Scorecard", key=f"save_sc_{job_id}"):
                    try:
                        parsed = json.loads(edited_json)
                        lock_p = str(sc_path) + ".lock"
        
                        with filelock.FileLock(lock_p, timeout=30):
                            sc_path.write_text(
                                json.dumps(parsed, indent=2, ensure_ascii=False),
                                encoding="utf-8"
                            )
                        st.success("✅ Scorecard saved.")
                        st.rerun()
                    except json.JSONDecodeError as e:
                        st.error(f"❌ Invalid JSON: {e}")
        else:
            st.info("No scorecard generated yet for this job.")

        if has_providers:
            btn_label = "🔄 Regenerate Scorecard" if scorecard_data else "📋 Generate Scorecard"
            if st.button(btn_label, key=f"gen_sc_{job_id}",
                         type="secondary" if scorecard_data else "primary"):
                with st.spinner("Generating scorecard from job description..."):
                    jd_clean = llm_scorer._strip_notes_section(md_text)
                    if jd_clean.strip():
                        sc = llm_scorer.generate_scorecard(
                            jd_clean, providers,
                            stage_params=stage_params_map.get("scorecard"),
                        )
                        if sc:
                            sc_dir.mkdir(parents=True, exist_ok=True)
                            lock_p = str(sc_path) + ".lock"
            
                            with filelock.FileLock(lock_p, timeout=30):
                                sc_path.write_text(
                                    json.dumps(sc, indent=2, ensure_ascii=False),
                                    encoding="utf-8"
                                )
                            st.success("✅ Scorecard generated!")
                            st.rerun()
                        else:
                            st.error("❌ Scorecard generation failed. Check terminal.")
                    else:
                        st.warning("Job description is empty.")

        st.divider()

        # ════════════════════════════════════════════════════════
        # SECTION B: Resume Evaluation
        # ════════════════════════════════════════════════════════
        st.markdown("#### 📊 Resume Evaluation")

        if shortcomings_data:
            sc_rel   = shortcomings_data.get("relevance", 0)
            sc_gaps  = shortcomings_data.get("shortcomings", [])
            sc_time  = shortcomings_data.get("evaluated_at", "")
            sc_hash  = shortcomings_data.get("resume_hash", "")

            col_score, col_info = st.columns([1, 3])
            with col_score:
                st.metric("Relevance", f"{sc_rel}/100")
            with col_info:
                st.caption(f"Evaluated: {sc_time[:19] if sc_time else 'N/A'}")
                # Staleness check (scorecard is JD-only; evaluation & gap depend on resume)
                if sc_hash and sc_hash != current_resume_hash:
                    st.warning(
                        "⚠️ Your resume has changed since this evaluation was generated. "
                        "Scores and shortcomings may be stale — click **Score My Resume** to update."
                    )
                if sc_gaps:
                    st.markdown("**Shortcomings:**")
                    for gap in sc_gaps:
                        st.markdown(f"- {gap}")
                else:
                    st.success("No shortcomings identified — strong match!")
                if "_meta" in shortcomings_data and shortcomings_data["_meta"]:
                    meta = shortcomings_data["_meta"]
                    st.caption(f"*(Evaluation generated by **{meta.get('model')}** via {meta.get('provider')})*")
        else:
            st.info("Resume not yet evaluated against this job.")

        if has_providers and scorecard_data:
            if st.button("📊 Score My Resume", key=f"eval_{job_id}",
                         type="secondary" if shortcomings_data else "primary"):
                with st.spinner("Evaluating resume against scorecard..."):
                    result = llm_scorer.score_job(
                        company, job_id, config,
                        scorecard_override=scorecard_data,
                        force=True,
                    )
                    if "error" in result:
                        st.error(f"❌ {result['error']}")
                    else:
                        st.success(f"✅ Score: {result['relevance']}/100")
                        st.cache_data.clear()
                        st.rerun()
        elif not scorecard_data and has_providers:
            st.caption("Generate a scorecard first to enable resume scoring.")

        st.divider()

        # ════════════════════════════════════════════════════════
        # SECTION C: Supplementary Gap Analysis
        # ════════════════════════════════════════════════════════
        st.markdown("#### 🔍 Supplementary Gap Analysis")

        if gap_data:
            gap_hash = gap_data.get("resume_hash", "")
            gap_time = gap_data.get("analyzed_at", "")
            st.caption(f"Analyzed: {gap_time[:19] if gap_time else 'N/A'}")
            if gap_hash and gap_hash != current_resume_hash:
                st.warning(
                    "⚠️ Your resume has changed since this gap analysis was generated. "
                    "Click **Check Supplementary Gaps** to refresh."
                )

            coverable   = gap_data.get("coverable", [])
            uncoverable = gap_data.get("uncoverable", [])

            if coverable:
                st.markdown(f"**✅ Coverable gaps ({len(coverable)}):**")
                for item in coverable:
                    st.markdown(f"- **{item.get('gap', '')}**")
                    st.caption(
                        f"  Evidence: {item.get('evidence', '')} "
                        f"(from {item.get('source_file', '')})"
                    )
            if uncoverable:
                st.markdown(f"**❌ Uncoverable gaps ({len(uncoverable)}):**")
                for gap in uncoverable:
                    st.markdown(f"- {gap}")
            if not coverable and not uncoverable:
                st.success("No gaps found — strong Supplementary alignment!")
            if "_meta" in gap_data and gap_data["_meta"]:
                meta = gap_data["_meta"]
                st.caption(f"*(Analysis generated by **{meta.get('model')}** via {meta.get('provider')})*")
        elif shortcomings_data and not shortcomings_data.get("shortcomings"):
            st.success("No shortcomings to check — resume is a strong match!")
        else:
            st.info("Gap analysis not yet run. Click below or use Express Pipeline.")

        if has_providers and shortcomings_data and shortcomings_data.get("shortcomings"):
            if st.button("🔍 Check Supplementary Gaps", key=f"gap_{job_id}"):
                with st.spinner("Analyzing Supplementary data against shortcomings..."):
                    supplementary_data = llm_scorer._read_supplementary_data(config)
                    current_supplementary_hash = hashlib.sha256(supplementary_data.encode()).hexdigest()[:12] if supplementary_data else ""
                    gap_result = llm_scorer.check_supplementary_gaps(
                        shortcomings_data["shortcomings"],
                        supplementary_data,
                        providers,
                        stage_params=stage_params_map.get("gap_analysis"),
                    )
                    if gap_result:
                        _save_gap(gap_result, current_supplementary_hash)
                        st.success("✅ Gap analysis complete — results saved.")
                        st.rerun()
                    else:
                        st.error("❌ Gap analysis failed.")
        elif not shortcomings_data:
            st.caption("Score your resume first to enable Supplementary gap analysis.")


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main() -> None:

    # ── URL Routing — job detail page ────────────────────────────────────────
    qp_company = st.query_params.get("company")
    qp_job_id  = st.query_params.get("job_id")
    if qp_company and qp_job_id:
        render_job_detail_page(qp_company, qp_job_id)
        return

    # ── Load data ───────────────────────────────────────────────────────────
    df, df_load_time = load_all_jobs()
    if df.empty:
        st.warning(
            "No job data loaded.  \n"
            "Run `python src/state_tracker.py` first to populate the tracker."
        )
        st.stop()

    all_companies = sorted(df["_company"].unique().tolist())

    # ── Sidebar ─────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("## ⚙️ Controls")

        company_filter = st.multiselect(
            "Company", options=all_companies, default=all_companies,
        )
        search_text = st.text_input("🔍 Search", placeholder="Title, ID, keyword...")

        st.divider()
        st.markdown("**Sort Priority**")
        sort1 = st.selectbox("Primary",   SORT_OPTIONS, index=0)
        sort2 = st.selectbox("Secondary", SORT_OPTIONS, index=1)

        st.divider()
        deadline_window = st.slider("⏰ Deadline alert window (days)", 1, 30, 7)
        limit_rows = st.number_input("🎯 Limit Active Radar rows", min_value=0, max_value=5000, value=0, step=10, help="0 means no limit")

        st.divider()
        # ── Run Scraper button ───────────────────────────────────────────────
        if st.button("🚀 Run Scraper", width="stretch",
                     help="Fetch latest jobs from all configured ATS sources in the background"):
            try:
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"
                
                log_dir = Path("logs")
                log_dir.mkdir(exist_ok=True)
                log_file = open(log_dir / "scraper.log", "a", encoding="utf-8")
                
                # Fire and forget
                subprocess.Popen(
                    [sys.executable, "src/state_tracker.py"],
                    cwd=str(_PROJECT_ROOT),
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT
                )
                st.success("🚀 **Scraper started in the background!** You can safely close this tab. The UI will automatically alert you when new jobs are ready.")
            except Exception as exc:
                st.error(f"❌ Failed to start scraper: {exc}")

        if st.button("🔄 Refresh Data", width="stretch",
                     help="Force reload all CSVs from disk"):
            st.cache_data.clear()
            st.rerun()

        st.divider()
        if st.button("🧹 Prune Dead Jobs", width="stretch",
                     help="Permanently delete files and CSV rows for archived/skipped unapplied jobs"):
            with st.spinner("Pruning dead jobs..."):
                try:
                    env = os.environ.copy()
                    env["PYTHONIOENCODING"] = "utf-8"
                    result = subprocess.run(
                        [sys.executable, "src/state_tracker.py", "--prune"],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        env=env,
                        check=True,
                        cwd=str(_PROJECT_ROOT),
                    )
                    output = result.stdout.strip()
                    st.success(f"✅ {output.splitlines()[-1] if output else 'Prune complete.'}")
                    st.cache_data.clear()
                    st.rerun()
                except subprocess.CalledProcessError as exc:
                    st.error(
                        f"❌ Prune failed (code {exc.returncode}):\n"
                        f"```\n{exc.stderr[-400:] if exc.stderr else 'No stderr output.'}\n```"
                    )

    # ── Base filter ─────────────────────────────────────────────────────────
    base = df[df["_company"].isin(company_filter)].copy()
    if search_text:
        mask = (
            base["title"].str.contains(search_text, case=False, na=False)
            | base["job_id"].str.contains(search_text, case=False, na=False)
        )
        base = base[mask]

    # Four-way split
    active_df   = _apply_sort(
        base[
            base["visible_bool"]
            & (~base["applied_bool"])
            & (~base["skipped_bool"])
        ],
        sort1, sort2,
    )
    if limit_rows > 0:
        active_df = active_df.head(limit_rows)
    sent_df     = _apply_sort(
        base[base["applied_bool"]],
        sort1, sort2,
    )
    archived_df = base[
        (~base["visible_bool"]) & (~base["applied_bool"])
    ].reset_index(drop=True)
    skipped_df  = base[
        base["skipped_bool"] & (~base["applied_bool"])
    ].reset_index(drop=True)

    # ── Header & Funnel Metrics ──────────────────────────────────────────────
    today          = pd.Timestamp.today().normalize()
    deadlines_soon = int(
        (
            base["last_date"].notna()
            & ((base["last_date"] - today).dt.days.between(0, deadline_window))
        ).sum()
    )

    # Read last scrape time from CLI metadata (Auto-refreshing fragment)

    
    # Use st.fragment (or experimental_fragment) if available for background auto-refresh
    fragment_decorator = getattr(st, "fragment", getattr(st, "experimental_fragment", None))
    
    def _render_scrape_status():
        last_scrape_time = None
        config_meta = config_engine.load_config("config.yaml")
        base_dir_meta = Path(config_meta["global_settings"]["output_base_dir"])
        metadata_path = base_dir_meta / "scrape_metadata.json"
        if metadata_path.exists():
            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
                if "last_scrape" in data:
                    last_scrape_time = pd.Timestamp(data["last_scrape"])
            except Exception:
                pass
    
        if last_scrape_time:
            hours_since = (pd.Timestamp.now(tz=datetime.timezone.utc) - last_scrape_time).total_seconds() / 3600
            
            # Check if background scrape happened after our UI data was loaded
            if last_scrape_time.timestamp() > df_load_time:
                st.warning("🔄 **New Data Available:** A scrape finished in the background! Click **🔄 Refresh Data** in the sidebar to load the latest jobs.")
            elif hours_since > 4:
                st.warning(f"⚠️ **Stale Data Alert:** It has been **{hours_since:.1f} hours** since your last scrape. Click **🚀 Run Scraper** in the sidebar to fetch fresh jobs.")
            else:
                st.caption(f"Last fetched data **{hours_since:.1f} hours ago**.")
                
    if fragment_decorator:
        # Auto-refresh this specific block every 60 seconds
        fragment_decorator(run_every=60)(_render_scrape_status)()
    else:
        _render_scrape_status()

    c_title, m1, m2, m3, m4 = st.columns([1.5, 1, 1, 1, 1], vertical_alignment="bottom")
    with c_title:
        st.markdown("### 🎯 Job Application OS")
        st.caption("Powered by the Workday CXS pipeline")

    m1.metric("🎯 Active Radar",      len(active_df))
    m2.metric("📤 Sent",              len(sent_df))
    m3.metric(
        f"⏰ Deadlines ≤{deadline_window}d",
        deadlines_soon,
        delta="urgent" if deadlines_soon else None,
        delta_color="inverse" if deadlines_soon else "off",
    )
    m4.metric("🏢 Companies", len(company_filter))

    # ── Tabs ────────────────────────────────────────────────────────────────
    tab_active, tab_sent, tab_archive, tab_manual, tab_insights, tab_settings = st.tabs([
        f"🎯 Active Radar  ({len(active_df)})",
        f"📤 Sent Applications  ({len(sent_df)})",
        f"📦 Archive & Skipped  ({len(archived_df) + len(skipped_df)})",
        "➕ Manual Entry",
        "🧠 Insights & Growth",
        "⚙️ Settings & Files",
    ])

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # TAB 1 — Active Radar
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab_active:
        if "manual_action_success" in st.session_state:
            st.success(st.session_state.pop("manual_action_success"))
            
        if active_df.empty:
            st.info("No active jobs match your filters. Try broadening your search.")
        else:
            select_all = st.checkbox("☑️ Select All Visible", key="select_all_t1")
            t1_source = active_df[_ACTIVE_BASE[1:]].copy().reset_index(drop=True)
            t1_source.insert(0, "selected", select_all)

            with st.form("tab1_bulk_form"):
                edited = st.data_editor(
                    t1_source,
                    column_config={k: v for k, v in _COL_CFG.items() if k in t1_source.columns},
                    disabled=_COMMON_COLS,
                    width="stretch",
                    hide_index=True,
                    num_rows="fixed",
                    key="tab1_editor",
                )
                
                col1, col2, col3, col4, col5 = st.columns([1.2, 1, 1, 1.2, 3])
                submit_applied = col1.form_submit_button("✅ Mark Applied", type="primary", help="Move selected jobs to the Sent Applications tab.")
                submit_skipped = col2.form_submit_button("🚫 Mark Skipped", help="Move selected jobs to the Archive & Skipped tab.")
                submit_score   = col3.form_submit_button("🤖 Bulk Score", help="Run LLM evaluation on all selected jobs.")
                submit_delete  = col4.form_submit_button("🗑️ Delete (Manual)", help="Permanently delete selected Manual jobs. Does not work on Workday jobs.")
                submit_save    = col5.form_submit_button("💾 Save Relevance Edits", help="Save inline edits made to the Relevance column without moving the jobs.")

            if submit_delete:
                selected_jobs = [active_df.iloc[i] for i in range(len(edited)) if edited.iloc[i]["selected"]]
                if selected_jobs:
                    non_manual = [j for j in selected_jobs if j["_company"] != "Manual"]
                    if non_manual:
                        st.error("❌ Bulk Delete is strictly restricted to Manual jobs. Please deselect Workday jobs.")
                    else:
                        config_del = config_engine.load_config("config.yaml")
                        base_dir_del = Path(config_del["global_settings"]["output_base_dir"])
                        manual_root = base_dir_del / "Manual"
                        csv_path = manual_root / "master_jobs.csv"
                        
                        job_ids_to_delete = {row["job_id"] for row in selected_jobs}
                        
                        # Delete files
                        for job_id in job_ids_to_delete:
                            md_path = manual_root / "job_details" / f"job_{job_id}.md"
                            sc_path = manual_root / "scorecards" / f"job_{job_id}.scorecard.json"
                            sh_path = manual_root / "shortcomings" / f"job_{job_id}.shortcomings.json"
                            
                            for p in [md_path, sc_path, sh_path]:
                                if p.exists():
                                    try:
                                        p.unlink()
                                    except Exception:
                                        pass
                                        
                        # Update CSV once
                        if csv_path.exists():

            
                            lock_path = str(csv_path) + ".lock"
                            with filelock.FileLock(lock_path, timeout=30):
                                ledger = {}
                                with csv_path.open("r", encoding="utf-8") as f:
                                    for r in csv.DictReader(f):
                                        if r["job_id"] not in job_ids_to_delete:
                                            ledger[r["job_id"]] = r
                                            
                                with csv_path.open("w", newline="", encoding="utf-8") as f:
                                    writer = csv.DictWriter(f, fieldnames=config_engine.CSV_COLUMNS, extrasaction="ignore")
                                    writer.writeheader()
                                    for r in ledger.values():
                                        writer.writerow(r)
                                        
                        st.session_state["manual_action_success"] = f"Deleted {len(selected_jobs)} manual job(s)."
                        st.cache_data.clear()
                        st.rerun()

            if submit_score:
                selected_jobs = [active_df.iloc[i] for i in range(len(edited)) if edited.iloc[i]["selected"]]
                if selected_jobs:
                    config = config_engine.load_config("config.yaml")
                    with st.status("Bulk Scoring...", expanded=True) as status:
                        for i, row in enumerate(selected_jobs):
                            st.write(f"Scoring {i+1}/{len(selected_jobs)}: {row['job_id']}")
                            res = llm_scorer.score_job(row['_company'], row['job_id'], config)
                            if "error" in res:
                                st.write(f"  ❌ {res['error']}")
                            else:
                                st.write(f"  ✅ Score: {res['relevance']}/100")
                        status.update(label="Bulk Scoring Complete!", state="complete")
                    st.cache_data.clear()
                    st.rerun()

            if submit_applied or submit_skipped or submit_save:
                changes: list[dict] = []
                for idx in range(min(len(edited), len(t1_source))):
                    orig     = t1_source.iloc[idx]
                    edit     = edited.iloc[idx]
                    full_row = active_df.iloc[idx]

                    # 1. Relevance changes
                    if int(edit["relevance"]) != int(orig["relevance"]):
                        changes.append({
                            "job_id":   full_row["job_id"],
                            "csv_path": full_row["_csv_path"],
                            "field":    "relevance",
                            "value":    int(edit["relevance"]),
                        })
                    
                    # 2. Bulk Actions
                    if edit["selected"]:
                        if submit_applied:
                            changes.append({
                                "job_id":   full_row["job_id"],
                                "csv_path": full_row["_csv_path"],
                                "field":    "applied",
                                "value":    datetime.date.today().isoformat(),
                            })
                        elif submit_skipped:
                            changes.append({
                                "job_id":   full_row["job_id"],
                                "csv_path": full_row["_csv_path"],
                                "field":    "skipped",
                                "value":    "yes",
                            })

                if changes:
                    _write_back(changes)


    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # TAB 2 — Sent Applications (editable — uncheck "App" to undo)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab_sent:
        if sent_df.empty:
            st.info(
                "No applications tracked yet.  \n"
                "Tick **App** in the Active Radar tab to log an application."
            )
        else:
            st.caption("Uncheck **App** to return a job to Active Radar.")
            t2_source = sent_df[_SENT_BASE[1:]].copy().reset_index(drop=True)
            t2_source.insert(0, "selected", False)

            with st.form("tab2_bulk_form"):
                edited_sent = st.data_editor(
                    t2_source,
                    column_config={k: v for k, v in _COL_CFG.items() if k in t2_source.columns},
                    disabled=_COMMON_COLS + ["applied"],
                    width="stretch",
                    hide_index=True,
                    num_rows="fixed",
                    key="tab2_editor",
                )
                
                col1, col2, col3 = st.columns([1, 1, 4])
                submit_undo = col1.form_submit_button("↩️ Undo Applied", help="Move selected jobs back to Active Radar.")
                submit_reject = col2.form_submit_button("🚫 Mark as Rejected (Skip)", help="Move selected jobs to Archive & Skipped.")
                submit_save_rel = col3.form_submit_button("💾 Save Relevance Edits", help="Save inline edits made to the Relevance column.")

            if submit_undo or submit_save_rel or submit_reject:
                changes: list[dict] = []
                for idx in range(min(len(edited_sent), len(t2_source))):
                    orig     = t2_source.iloc[idx]
                    edit     = edited_sent.iloc[idx]
                    full_row = sent_df.iloc[idx]
                    
                    if int(edit["relevance"]) != int(orig["relevance"]):
                        changes.append({
                            "job_id":   full_row["job_id"],
                            "csv_path": full_row["_csv_path"],
                            "field":    "relevance",
                            "value":    int(edit["relevance"]),
                        })

                    if edit["selected"]:
                        if submit_undo:
                            changes.append({
                                "job_id":   full_row["job_id"],
                                "csv_path": full_row["_csv_path"],
                                "field":    "applied",
                                "value":    "",
                            })
                        elif submit_reject:
                            changes.append({
                                "job_id":   full_row["job_id"],
                                "csv_path": full_row["_csv_path"],
                                "field":    "applied",
                                "value":    "",
                            })
                            changes.append({
                                "job_id":   full_row["job_id"],
                                "csv_path": full_row["_csv_path"],
                                "field":    "skipped",
                                "value":    "yes",
                            })
                if changes:
                    _write_back(changes)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # TAB 3 — Archive & Skipped
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab_archive:
        st.markdown("### 🚫 Skipped Jobs (Live)")
        if skipped_df.empty:
            st.info(
                "No skipped jobs.  \n"
                "Tick **🚫** in the Active Radar tab to skip a job."
            )
        else:
            st.caption("Uncheck **🚫** to restore a job to Active Radar.")
            t4_source = skipped_df[_SKIPPED_BASE[1:]].copy().reset_index(drop=True)
            t4_source.insert(0, "selected", False)

            with st.form("tab4_bulk_form"):
                edited_skip = st.data_editor(
                    t4_source,
                    column_config={k: v for k, v in _COL_CFG.items() if k in t4_source.columns},
                    disabled=_COMMON_COLS,
                    width="stretch",
                    hide_index=True,
                    num_rows="fixed",
                    key="tab4_editor",
                )
                submit_undo_skip = st.form_submit_button("↩️ Restore Selected to Active Radar", help="Move selected jobs back to the Active Radar tab.")

            if submit_undo_skip:
                changes: list[dict] = []
                for idx in range(min(len(edited_skip), len(t4_source))):
                    edit     = edited_skip.iloc[idx]
                    full_row = skipped_df.iloc[idx]
                    
                    if edit["selected"]:
                        changes.append({
                            "job_id":   full_row["job_id"],
                            "csv_path": full_row["_csv_path"],
                            "field":    "skipped",
                            "value":    "",
                        })
                if changes:
                    _write_back(changes)

        st.divider()
        st.markdown("### 📦 Delisted Jobs (Dead)")
        if archived_df.empty:
            st.info(
                "No archived jobs yet.  \n"
                "Jobs appear here when removed from the live board on the next scraper run."
            )
        else:
            st.caption("Read-only. Click 🔗 to view the cached Job Description in a new tab.")
            t3_source = archived_df[_ARCHIVED_BASE].copy().reset_index(drop=True)

            st.data_editor(
                t3_source,
                column_config={k: v for k, v in _COL_CFG.items() if k in t3_source.columns},
                disabled=[c for c in _ARCHIVED_BASE if c in t3_source.columns],
                width="stretch",
                hide_index=True,
                num_rows="fixed",
                key="tab3_editor",
            )


    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # TAB 4 — Manual Entry
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab_manual:
        st.markdown("### ➕ Add Manual Job")
        st.caption("Track external applications (e.g., from LinkedIn or Indeed).")
        
        if "manual_success" in st.session_state:
            st.success(st.session_state.pop("manual_success"))
            
        with st.form("manual_job_form", clear_on_submit=True):
            man_company = st.text_input("Company Name *", placeholder="e.g., Google")
            man_title   = st.text_input("Job Title *", placeholder="e.g., Software Engineer")
            man_url     = st.text_input("Job URL (optional)", placeholder="https://...")
            
            c1, c2 = st.columns(2)
            man_posted   = c1.date_input("Posted Date *", value="today")
            man_deadline = c2.date_input("Deadline (optional)", value=None)
            
            man_desc = st.text_area("Job Description *", height=300, placeholder="Paste the full job description here...")
            
            submit_manual = st.form_submit_button("💾 Save Manual Job", type="primary")
            
        if submit_manual:
            if not man_company.strip() or not man_title.strip() or not man_desc.strip():
                st.error("Company, Title, and Description are required.")
            else:

                manual_id = f"MANUAL-{uuid.uuid4().hex[:8].upper()}"
                
                # Format markdown
                url_line = f"| **URL**          | {man_url} |\n" if man_url.strip() else ""
                deadline_line = f"| **Deadline**     | {man_deadline.isoformat()} |\n" if man_deadline else ""
                
                content = f"# [{man_company.strip()}] {man_title.strip()}\n\n"
                content += "| Field            | Value |\n"
                content += "|------------------|-------|\n"
                content += f"| **Company**      | {man_company.strip()} |\n"
                content += f"| **Posted Date**  | {man_posted.isoformat()} |\n"
                content += deadline_line
                content += url_line
                content += "\n---\n\n## Job Description\n\n"
                content += man_desc
                
                # Write files
                config_add = config_engine.load_config("config.yaml")
                base_dir_add = Path(config_add["global_settings"]["output_base_dir"])
                manual_root = base_dir_add / "Manual"
                jd_dir = manual_root / "job_details"
                jd_dir.mkdir(parents=True, exist_ok=True)
                
                md_path = jd_dir / f"job_{manual_id}.md"
                md_path.write_text(content, encoding="utf-8")
                
                csv_path = manual_root / "master_jobs.csv"
                ledger = {}

                if csv_path.exists():
                    with csv_path.open("r", encoding="utf-8") as f:
                        for row in csv.DictReader(f):
                            ledger[row["job_id"]] = row
                            
                ledger[manual_id] = {
                    "job_id": manual_id,
                    "title": f"[{man_company.strip()}] {man_title.strip()}",
                    "first_discovered_on": man_posted.isoformat(),
                    "last_date": man_deadline.isoformat() if man_deadline else "",
                    "visible": "yes",
                    "relevance": "0",
                    "applied": "",
                    "skipped": ""
                }
                

                lock_path = str(csv_path) + ".lock"
                with filelock.FileLock(lock_path, timeout=30):
                    with csv_path.open("w", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=config_engine.CSV_COLUMNS, extrasaction="ignore")
                        writer.writeheader()
                        for r in ledger.values():
                            writer.writerow(r)
                            
                st.session_state["manual_success"] = f"Successfully added manual job: {manual_id}"
                st.cache_data.clear()
                st.rerun()

    # TAB 5 — Insights & Growth
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab_insights:
        st.markdown("### 🧠 Career Insights & Growth Hub")
        st.caption("Aggregates all job shortcomings into unified missing skills, and checks them against your Supplementary Data.")

        config = config_engine.load_config("config.yaml")
        profile_cfg = config.get("user_profile") or {}
        resume_path = Path(profile_cfg.get("resume_path", "user_details/resume.md"))
        user_details_dir = resume_path.parent
        digested_path = user_details_dir / "digested_insights.json"
        cache_path = user_details_dir / "gap_fill_cache.json"
        
        current_resume_hash = "unknown"
        if resume_path.exists():
            current_resume_hash = hashlib.sha256(resume_path.read_text(encoding="utf-8").encode()).hexdigest()[:12]
            
        providers, stage_params_map = llm_client.load_llm_config(config)
        has_providers = len(providers) > 0

        # --- Top Section: Actionable Data ---
        cache_data = None
        if cache_path.exists():
            try:
                cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
                
        if cache_data and "quick_wins" in cache_data:
            meta = cache_data.get("_meta", {})
            provider_str = meta.get("provider", "Unknown")
            model_str = meta.get("model", "Unknown")
            st.caption(f"**Last Gap Fill Run:** {cache_data.get('last_run_date', 'Unknown')[:19]} | **Model:** {provider_str} / {model_str}")
            if cache_data.get("resume_hash") != current_resume_hash:
                st.warning("🔄 **Resume has changed since last run.** Your insights might be outdated. Re-run analysis below.")
            
            col_qw, col_lp = st.columns(2)
            with col_qw:
                st.markdown("#### ⚡ Quick Wins (Resume Updates)")
                st.info("You already possess these skills based on your Supplementary Data. Add them to your resume immediately.")
                for item in cache_data.get("quick_wins", []):
                    with st.expander(f"**{item.get('skill', '')}** (Missing in {item.get('count', 0)} jobs)"):
                        st.write(f"**Evidence:** {item.get('evidence', '')}")
            with col_lp:
                st.markdown("#### 📚 Learning Path (True Gaps)")
                st.warning("You truly lack these skills. Focus your learning here to maximize employability.")
                for item in cache_data.get("learning_path", []):
                    with st.expander(f"**{item.get('skill', '')}** (Missing in {item.get('count', 0)} jobs)"):
                        st.write(f"**Context:** {item.get('reason', '')}")
        else:
            st.info("No actionable data yet. Run the analysis below.")

        st.markdown("---")

        # Load digested insights
        digested = {}
        if digested_path.exists():
            try:
                digested = json.loads(digested_path.read_text(encoding="utf-8"))
            except Exception:
                pass
                
        # Load raw shortcomings for prompt export (from active and archive ledgers)
        ledger_path = user_details_dir / "skill_gaps_ledger.jsonl"
        archive_path = user_details_dir / "skill_gaps_ledger_archive.jsonl"
        all_gap_entries = []
        
        for p in [ledger_path, archive_path]:
            if p.exists():
                with p.open("r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line.strip())
                            if entry.get("resume_hash") == current_resume_hash:
                                sh = entry.get("shortcomings", [])
                                if sh:
                                    all_gap_entries.append({
                                        "evaluated_at": entry.get("evaluated_at", ""),
                                        "company": entry.get("company", "Unknown"),
                                        "job_id": entry.get("job_id", "Unknown"),
                                        "shortcomings": sh
                                    })
                        except Exception:
                            pass
                            
        # Sort newest first
        all_gap_entries.sort(key=lambda x: x["evaluated_at"], reverse=True)

        # --- Middle Section: Historical Misses & Export ---
        
        col_mid1, col_mid2 = st.columns(2)
        
        with col_mid1:
            past_hashes = [h for h in digested.keys() if h != current_resume_hash]
            if past_hashes:
                with st.expander("🕰️ Historical Misses (Past Resumes)", expanded=False):
                    st.write("Skills identified as missing in older versions of your resume:")
                    all_past_skills = set()
                    for ph in past_hashes:
                        for s in digested[ph].get("clustered_skills", []):
                            all_past_skills.add(s.get("skill", ""))
                    
                    if all_past_skills:
                        badges = " ".join([f"`{s}`" for s in sorted(list(all_past_skills)) if s])
                        st.markdown(badges)
                    else:
                        st.write("None.")
                        
        with col_mid2:
            if all_gap_entries:
                with st.expander("📥 Export Raw Gaps for External AI", expanded=False):
                    st.write("Use this prompt with ChatGPT, Claude, or another powerful AI to manually cluster your shortcomings if the local models fail.")
                    
                    bullets_blocks = []
                    for entry in all_gap_entries:
                        ts = entry["evaluated_at"][:10] if entry["evaluated_at"] else "Unknown Date"
                        company = entry["company"]
                        job_id = entry["job_id"]
                        
                        lines = [f"# {ts} | {company} ({job_id})"]
                        for s in entry["shortcomings"]:
                            lines.append(f"- {s}")
                        bullets_blocks.append("\n".join(lines))
                        
                    bullets = "\n".join(bullets_blocks)
                    
                    prompt_text = (
                        "You are a career data analyst. Below is a chronological list of shortcomings "
                        "(newest first) identified across my recent job applications. Please cluster "
                        "similar shortcomings into unified skills and calculate their frequency.\n\n"
                        "Output ONLY a flat JSON array of objects with the exact structure:\n"
                        "[\n  {\n    \"skill\": \"<Unified Skill Name>\",\n    \"count\": <integer frequency>\n  }\n]\n\n"
                        "Rules:\n"
                        "1. Group similar technologies cohesively.\n"
                        "2. You must be EXHAUSTIVE. Do not ignore any distinct skill.\n"
                        "3. DO NOT use chain of thought. Output ONLY the JSON.\n"
                        "4. Ignore the comment lines starting with '#'.\n\n"
                        "## Raw Shortcomings to Cluster\n"
                        f"{bullets}"
                    )
                    
                    st.download_button(
                        label="Download Prompt as .txt",
                        data=prompt_text,
                        file_name="manual_clustering_prompt.txt",
                        mime="text/plain"
                    )
                    st.code(prompt_text, language="markdown")

        st.markdown("---")

        # --- Bottom Section: Advanced Editor ---
        st.markdown("### ⚙️ Advanced Editor (Clustered Gaps)")
        
        all_hashes = list(digested.keys())
        # Sort hashes by last_updated descending
        all_hashes.sort(key=lambda x: digested.get(x, {}).get("last_updated", ""), reverse=True)
        if current_resume_hash not in all_hashes:
            all_hashes.insert(0, current_resume_hash)
            
        selected_hash = st.selectbox(
            "Select Resume Version to view/edit missing skills JSON:", 
            options=all_hashes,
            format_func=lambda x: f"{x} (Current Resume)" if x == current_resume_hash else x
        )
        
        is_current = (selected_hash == current_resume_hash)
        
        # Determine json string to display
        active_json_obj = digested.get(selected_hash, {}).get("clustered_skills", [])
        active_json_str = json.dumps(active_json_obj, indent=2)
        
        with st.form("editor_form"):
            edited_json = st.text_area(
                "Raw `{skill, count}` JSON array",
                value=active_json_str,
                height=300,
                help="You can manually edit this array to fix LLM clustering mistakes or port over old skills."
            )
            
            col_btn1, col_btn2, col_btn3 = st.columns(3)
            with col_btn1:
                save_clicked = st.form_submit_button("💾 Save JSON", disabled=not is_current)
            with col_btn2:
                run_clicked = st.form_submit_button("🚀 Process Ledger & Gap Fill", disabled=not is_current or not has_providers)
            with col_btn3:
                run_only_clicked = st.form_submit_button("⚡ Gap Fill ONLY (Skip Ledger)", disabled=not is_current or not has_providers)
                
            if save_clicked and is_current:
                try:
                    parsed = json.loads(edited_json)
                    if not isinstance(parsed, list):
                        st.error("❌ Invalid format. Please ensure you paste a valid JSON array (a list of objects) into the editor above.")
                    else:
                        if selected_hash not in digested:
                            digested[selected_hash] = {}
                        digested[selected_hash]["clustered_skills"] = parsed
                        digested[selected_hash]["last_updated"] = datetime.datetime.now().isoformat()
                        lock_p = str(digested_path) + ".lock"
                        with filelock.FileLock(lock_p, timeout=30):
                            digested_path.write_text(json.dumps(digested, indent=2), encoding="utf-8")
                        st.success("JSON saved successfully!")
                        st.rerun()
                except Exception as e:
                    st.error(f"Invalid JSON: {e}")
                    
            if run_clicked and is_current:
                with st.spinner("Processing ledger and running MapReduce Gap Fill..."):
                    # 1. Digest active ledger first
                    updated_digested = llm_scorer.digest_ledger(config, providers, stage_params_map.get("global_insights"))
                    
                    # 2. Get current resume clustered skills
                    clustered = updated_digested.get(current_resume_hash, {}).get("clustered_skills", [])
                    
                    # 3. Gap Fill
                    result = llm_scorer.run_gap_fill(clustered, config, providers, stage_params_map.get("global_gap_fill"))
                    if result:
                        cache_payload = {
                            "last_run_date": datetime.datetime.now().isoformat(),
                            "resume_hash": current_resume_hash,
                            "quick_wins": result.get("quick_wins", []),
                            "learning_path": result.get("learning_path", []),
                            "_meta": result.get("_meta", {})
                        }
                        lock_p = str(cache_path) + ".lock"
                        with filelock.FileLock(lock_p, timeout=30):
                            cache_path.write_text(json.dumps(cache_payload, indent=2, ensure_ascii=False), encoding="utf-8")
                        st.success("✅ Gap Fill Analysis complete!")
                        st.rerun()
                    else:
                        st.error("❌ Failed to generate gap fill analysis. Check logs.")
                        
            if run_only_clicked and is_current:
                with st.spinner("Running MapReduce Gap Fill (Skipping Ledger)..."):
                    # 1. Get current resume clustered skills directly from UI/Disk
                    clustered = digested.get(current_resume_hash, {}).get("clustered_skills", [])
                    
                    if not clustered:
                        st.error("❌ Clustered skills list is empty. Please paste your JSON into the editor above and click '💾 Save JSON' before running this step.")
                    else:
                        # 2. Gap Fill
                        result = llm_scorer.run_gap_fill(clustered, config, providers, stage_params_map.get("global_gap_fill"))
                        if result:
                            cache_payload = {
                                "last_run_date": datetime.datetime.now().isoformat(),
                                "resume_hash": current_resume_hash,
                                "quick_wins": result.get("quick_wins", []),
                                "learning_path": result.get("learning_path", []),
                                "_meta": result.get("_meta", {})
                            }
                            lock_p = str(cache_path) + ".lock"
                            with filelock.FileLock(lock_p, timeout=30):
                                cache_path.write_text(json.dumps(cache_payload, indent=2, ensure_ascii=False), encoding="utf-8")
                            st.success("✅ Gap Fill Analysis complete!")
                            st.rerun()
                        else:
                            st.error("❌ Failed to generate gap fill analysis. Check logs.")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # TAB 6 — Settings & Files
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab_settings:
        st.markdown("### ⚙️ Cloud File Manager")
        st.caption("Edit your base configuration and profile data directly on the server without SSH.")
        
        config_path = Path("config.yaml")
        profile_cfg = config.get("user_profile") or {}
        resume_path = Path(profile_cfg.get("resume_path", "user_details/resume.md"))
        custom_notes_path = Path(profile_cfg.get("custom_notes_path", "user_details/custom_notes.md"))
        supp_dir = Path(profile_cfg.get("supplementary_dir", "user_details/SupplementaryData"))
        
        editable_files = {
            "config.yaml": config_path,
            "Resume (Markdown)": resume_path,
            "Custom Notes": custom_notes_path,
        }
        
        if supp_dir.exists() and supp_dir.is_dir():
            for f in supp_dir.glob("*.txt"):
                editable_files[f"Supplementary: {f.name}"] = f
                
        selected_file_label = st.selectbox("Select file to edit", list(editable_files.keys()))
        selected_file_path = editable_files[selected_file_label]
        
        if selected_file_path.exists():
            file_content = selected_file_path.read_text(encoding="utf-8")
        else:
            file_content = ""
            st.warning(f"File {selected_file_path} does not exist yet. Saving will create it.")
            
        with st.form("file_editor_form"):
            new_content = st.text_area("File Content", value=file_content, height=500)
            if st.form_submit_button("💾 Save File"):
                selected_file_path.parent.mkdir(parents=True, exist_ok=True)
                lock_p = str(selected_file_path) + ".lock"
                with filelock.FileLock(lock_p, timeout=30):
                    selected_file_path.write_text(new_content, encoding="utf-8")
                st.success(f"Saved {selected_file_path.name} successfully!")
                st.cache_data.clear()
                st.rerun()

        st.markdown("---")
        st.markdown("### 🧹 State Management")
        with st.expander("Reset LLM Evaluation State", expanded=False):
            st.warning("⚠️ This will permanently delete all generated scorecards, shortcomings, and digested insights. Your parsed job descriptions and custom profile data will remain safe. Master ledger relevance will be reset to 0.")
            if st.button("🧨 Wipe All LLM State"):
                with st.spinner("Deleting state files..."):
                    all_paths = config_engine.resolve_output_paths(config)
                    deleted_files = 0
                    reset_csvs = 0
                    
                    for p in all_paths:
                        # Scorecards
                        scorecards_dir = p["scorecards_dir"]
                        if scorecards_dir.exists():
                            for f in scorecards_dir.glob("*.json"):
                                f.unlink()
                                deleted_files += 1
                        # Shortcomings
                        shortcomings_dir = p["shortcomings_dir"]
                        if shortcomings_dir.exists():
                            for f in shortcomings_dir.glob("*.json"):
                                f.unlink()
                                deleted_files += 1
                        # Reset Relevance in CSV
                        csv_path = p["root_dir"] / "master_jobs.csv"
                        if csv_path.exists():
                            try:
                                df = pd.read_csv(csv_path, dtype=str)
                                if "relevance" in df.columns:
                                    df["relevance"] = "0"
                                    lock_csv = str(csv_path) + ".lock"
                                    with filelock.FileLock(lock_csv, timeout=30):
                                        df.to_csv(csv_path, index=False)
                                    reset_csvs += 1
                            except Exception:
                                pass
                                
                    # Clear User Details insights artifacts
                    insights_files = ["digested_insights.json", "gap_fill_cache.json", "skill_gaps_ledger.jsonl", "skill_gaps_ledger_archive.jsonl"]
                    user_details_dir = resume_path.parent
                    if user_details_dir.exists():
                        for filename in insights_files:
                            file_path = user_details_dir / filename
                            if file_path.exists():
                                file_path.unlink()
                                deleted_files += 1
                                
                    st.success(f"✅ Deleted {deleted_files} state files and reset {reset_csvs} CSV ledgers.")
                    time.sleep(2)
                    st.cache_data.clear()
                    st.rerun()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
