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
from pathlib import Path

# Path injection — allows `import src.config_engine` from the project root
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import datetime
import re
from collections import defaultdict

import filelock
import pandas as pd
import streamlit as st

import src.config_engine as config_engine
import src.llm_client as llm_client
import src.llm_scorer as llm_scorer

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

SORT_OPTIONS = ["Deadline ↑", "Relevance ↓", "Posted Date ↓", "Company A→Z"]
_SORT_MAP = {
    "Deadline ↑":    ("last_date",             True),
    "Relevance ↓":   ("relevance",             False),
    "Posted Date ↓": ("first_discovered_on",   False),
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
def load_all_jobs() -> pd.DataFrame:
    """
    Concat master_jobs.csv from every configured company.
    Injects _company, _csv_path, _md_dir per-row (never displayed).
    TTL=30s keeps the cache fresh without a manual refresh.
    """
    config    = config_engine.load_config("config.yaml")
    all_paths = config_engine.resolve_output_paths(config)

    frames: list[pd.DataFrame] = []
    for p in all_paths:
        csv_path        = Path(p["root_dir"]) / "master_jobs.csv"
        job_details_dir = Path(p["job_details_dir"])
        if not csv_path.exists():
            continue
        df = pd.read_csv(csv_path, dtype=str).fillna("")
        df["_company"]  = p["name"]
        df["_csv_path"] = str(csv_path)
        df["_md_dir"]   = str(job_details_dir)
        frames.append(df)

    if not frames:
        return pd.DataFrame()

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
    combined["skipped_bool"] = combined["skipped"].apply(_is_skipped)
    combined["view_url"] = combined.apply(
        lambda r: f"/?company={r['_company']}&job_id={r['job_id']}", axis=1
    )
    return combined


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
    import hashlib

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
        title_line  = next((l.lstrip("# ").strip() for l in md_text_raw.splitlines() if l.startswith("#")), job_id)
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
        def _save_gap(gap_result: dict, linkedin_hash: str = "") -> None:
            gap_dir  = gap_path.parent
            gap_dir.mkdir(parents=True, exist_ok=True)
            gap_payload = {
                "job_id":      job_id,
                "company":     company,
                "analyzed_at": datetime.date.today().isoformat(),
                "resume_hash": current_resume_hash,
                "linkedin_hash": linkedin_hash,
                "coverable":   gap_result.get("coverable", []),
                "uncoverable": gap_result.get("uncoverable", []),
            }
            gap_path.write_text(
                json.dumps(gap_payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        # ════════════════════════════════════════════════════════
        # ⚡ EXPRESS PIPELINE (top-of-tab CTA)
        # ════════════════════════════════════════════════════════
        st.markdown("#### ⚡ Express Pipeline")
        st.caption("Runs all three stages sequentially: Scorecard → Resume Score → LinkedIn Gap Analysis")
        if has_providers:
            if st.button("⚡ Run Express Pipeline", type="primary", key=f"express_{job_id}",
                         use_container_width=True):
                _express_result: dict = {}

                with st.status("Running Express Pipeline...", expanded=True) as status:
                    # Fetch linkedin data & hash for stage 3
                    linkedin_data = llm_scorer._read_linkedin_data(config)
                    current_linkedin_hash = hashlib.sha256(linkedin_data.encode()).hexdigest()[:12] if linkedin_data else ""

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
                    st.write("🔍 Stage 3: LinkedIn Gap Analysis…")
                    if gap_data and gap_data.get("resume_hash") == current_resume_hash and gap_data.get("linkedin_hash") == current_linkedin_hash:
                        st.write("  ✅ Gap analysis already exists and is up-to-date — skipped.")
                    else:
                        gaps_to_check = _express_result.get("shortcomings", [])
                        if gaps_to_check:
                            gap_result = llm_scorer.check_linkedin_gaps(
                                gaps_to_check, linkedin_data, providers,
                                stage_params=stage_params_map.get("gap_analysis"),
                            )
                            if gap_result:
                                _save_gap(gap_result, current_linkedin_hash)
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
                        sc_path.write_text(
                            json.dumps(parsed, indent=2, ensure_ascii=False),
                            encoding="utf-8",
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
                            sc_path.write_text(
                                json.dumps(sc, indent=2, ensure_ascii=False),
                                encoding="utf-8",
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
        else:
            st.info("Resume not yet evaluated against this job.")

        if has_providers and scorecard_data:
            if st.button("📊 Score My Resume", key=f"eval_{job_id}",
                         type="secondary" if shortcomings_data else "primary"):
                with st.spinner("Evaluating resume against scorecard..."):
                    result = llm_scorer.score_job(
                        company, job_id, config,
                        scorecard_override=scorecard_data,
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
        # SECTION C: LinkedIn Gap Analysis
        # ════════════════════════════════════════════════════════
        st.markdown("#### 🔍 LinkedIn Gap Analysis")

        if gap_data:
            gap_hash = gap_data.get("resume_hash", "")
            gap_time = gap_data.get("analyzed_at", "")
            st.caption(f"Analyzed: {gap_time[:19] if gap_time else 'N/A'}")
            if gap_hash and gap_hash != current_resume_hash:
                st.warning(
                    "⚠️ Your resume has changed since this gap analysis was generated. "
                    "Click **Check LinkedIn Gaps** to refresh."
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
                st.success("No gaps found — strong LinkedIn alignment!")
        elif shortcomings_data and not shortcomings_data.get("shortcomings"):
            st.success("No shortcomings to check — resume is a strong match!")
        else:
            st.info("Gap analysis not yet run. Click below or use Express Pipeline.")

        if has_providers and shortcomings_data and shortcomings_data.get("shortcomings"):
            if st.button("🔍 Check LinkedIn Gaps", key=f"gap_{job_id}"):
                with st.spinner("Analyzing LinkedIn data against shortcomings..."):
                    linkedin_data = llm_scorer._read_linkedin_data(config)
                    current_linkedin_hash = hashlib.sha256(linkedin_data.encode()).hexdigest()[:12] if linkedin_data else ""
                    gap_result = llm_scorer.check_linkedin_gaps(
                        shortcomings_data["shortcomings"],
                        linkedin_data,
                        providers,
                        stage_params=stage_params_map.get("gap_analysis"),
                    )
                    if gap_result:
                        _save_gap(gap_result, current_linkedin_hash)
                        st.success("✅ Gap analysis complete — results saved.")
                        st.rerun()
                    else:
                        st.error("❌ Gap analysis failed.")
        elif not shortcomings_data:
            st.caption("Score your resume first to enable LinkedIn gap analysis.")


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
    df = load_all_jobs()


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

        st.divider()
        # ── Run Scraper button ───────────────────────────────────────────────
        if st.button("🚀 Run Scraper", width="stretch",
                     help="Fetch latest jobs from all configured ATS sources"):
            with st.spinner("Fetching latest jobs from ATS..."):
                try:
                    env = os.environ.copy()
                    env["PYTHONIOENCODING"] = "utf-8"
                    subprocess.run(
                        [sys.executable, "src/state_tracker.py"],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        env=env,
                        check=True,
                        cwd=str(_PROJECT_ROOT),
                    )
                    st.success("✅ Scraper completed.")
                    st.cache_data.clear()
                    st.rerun()
                except subprocess.CalledProcessError as exc:
                    st.error(
                        f"❌ Scraper exited with error (code {exc.returncode}):\n"
                        f"```\n{exc.stderr[-600:] if exc.stderr else 'No stderr output.'}\n```"
                    )

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
            (base["visible"].str.lower() == "yes")
            & base["applied"].apply(_not_applied)
            & base["skipped_bool"].eq(False)
        ],
        sort1, sort2,
    )
    sent_df     = _apply_sort(
        base[base["applied"].apply(_is_applied)],
        sort1, sort2,
    )
    archived_df = base[
        (base["visible"].str.lower() == "no") & base["applied"].apply(_not_applied)
    ].reset_index(drop=True)
    skipped_df  = base[
        base["skipped_bool"].eq(True) & base["applied"].apply(_not_applied)
    ].reset_index(drop=True)

    # ── Header & Funnel Metrics ──────────────────────────────────────────────
    today          = pd.Timestamp.today().normalize()
    deadlines_soon = int(
        (
            base["last_date"].notna()
            & ((base["last_date"] - today).dt.days.between(0, deadline_window))
        ).sum()
    )

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
    tab1, tab2, tab3, tab4 = st.tabs([
        f"🎯 Active Radar  ({len(active_df)})",
        f"📤 Sent Applications  ({len(sent_df)})",
        f"📦 Archived  ({len(archived_df)})",
        f"🚫 Skipped  ({len(skipped_df)})",
    ])

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # TAB 1 — Active Radar
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab1:
        if active_df.empty:
            st.info("No active jobs match your filters. Try broadening your search.")
        else:
            t1_source = active_df[_ACTIVE_BASE[1:]].copy().reset_index(drop=True)
            t1_source.insert(0, "selected", False)

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
                
                col1, col2, col3, col4 = st.columns([1, 1, 1, 3])
                submit_applied = col1.form_submit_button("✅ Mark Applied", type="primary")
                submit_skipped = col2.form_submit_button("🚫 Mark Skipped")
                submit_score   = col3.form_submit_button("🤖 Bulk Score")
                submit_save    = col4.form_submit_button("💾 Save Relevance")

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
    with tab2:
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
                
                col1, col2 = st.columns([1, 5])
                submit_undo = col1.form_submit_button("↩️ Undo Applied")
                submit_save_rel = col2.form_submit_button("💾 Save Relevance")

            if submit_undo or submit_save_rel:
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

                    if submit_undo and edit["selected"]:
                        changes.append({
                            "job_id":   full_row["job_id"],
                            "csv_path": full_row["_csv_path"],
                            "field":    "applied",
                            "value":    "",
                        })
                if changes:
                    _write_back(changes)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # TAB 3 — Archived (read-only except for 📖 open_modal)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab3:
        if archived_df.empty:
            st.info(
                "No archived jobs yet.  \n"
                "Jobs appear here when removed from the live board on the next scraper run."
            )
        else:
            st.caption("Read-only. Click 🔗 to view the cached Job Description in a new tab.")
            t3_source = archived_df[_ARCHIVED_BASE].copy().reset_index(drop=True)

            edited_arch = st.data_editor(
                t3_source,
                column_config={k: v for k, v in _COL_CFG.items() if k in t3_source.columns},
                disabled=[c for c in _ARCHIVED_BASE if c in t3_source.columns],
                width="stretch",
                hide_index=True,
                num_rows="fixed",
                key="tab3_editor",
            )


    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # TAB 4 — Skipped Jobs (un-skip by unchecking 🚫)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab4:
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
                submit_undo_skip = st.form_submit_button("↩️ Restore Selected to Active Radar")

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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
