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
    /* ── Hide Streamlit chrome ── */
    #MainMenu                    { visibility: hidden; }
    header                       { visibility: hidden; }
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

# Display column lists (open_modal injected at position 0 at render time)
_ACTIVE_BASE   = ["applied_bool", "relevance", "_company", "job_id", "title",
                  "first_discovered_on", "last_date"]
_SENT_BASE     = ["applied_bool", "applied", "relevance", "_company", "job_id", "title",
                  "first_discovered_on", "last_date"]
_ARCHIVED_BASE = ["_company", "job_id", "title", "first_discovered_on", "last_date"]

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
    "open_modal":          st.column_config.CheckboxColumn(
                               "📖",
                               help="Open full job details",
                               width="small",
                           ),
    "applied_bool":        st.column_config.CheckboxColumn(
                               "App",
                               help="Check to mark applied (saves today's date). Uncheck to undo.",
                               width="small",
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
# Job Details modal
# ---------------------------------------------------------------------------

@st.dialog("Job Details", width="large")
def show_job_modal(job_id: str, title: str, company: str, md_dir: str) -> None:
    st.markdown(f"### {title}")
    st.caption(f"**{company}**  ·  `{job_id}`")
    st.divider()

    md_path = Path(md_dir) / f"job_{job_id}.md"
    file_mtime = md_path.stat().st_mtime if md_path.exists() else 0
    notes_key = f"notes_input_{job_id}_{file_mtime}"

    def handle_save():
        if md_path.exists():
            current_md = md_path.read_text(encoding="utf-8")
            new_val = st.session_state.get(notes_key, "")
            updated_md = _write_notes(current_md, new_val)
            md_path.write_text(updated_md, encoding="utf-8")

    md_text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""

    tab_jd, tab_notes, tab_llm = st.tabs([
        "📄 Job Description",
        "✏️  My Notes",
        "🤖 LLM / Resume",
    ])

    with tab_jd:
        if md_text:
            st.markdown(md_text, unsafe_allow_html=False)
        else:
            st.warning(
                "No detail file found.  \n"
                "Run `python src/state_tracker.py` to generate `.md` files."
            )

    with tab_notes:
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
        if st.button("💾 Save Notes", type="primary", width="stretch", on_click=handle_save):
            st.success("✅ Notes saved to disk.")

    with tab_llm:
        # Load config for LLM operations
        _llm_config = config_engine.load_config("config.yaml")
        _providers, _stage_params = llm_client.load_llm_config(_llm_config)

        # Resolve paths for this job
        _all_paths = config_engine.resolve_output_paths(_llm_config)
        _path_map = {p["name"]: p for p in _all_paths}
        _safe_id = config_engine.sanitize_filename(job_id)

        _has_providers = len(_providers) > 0

        if company in _path_map:
            _scorecards_dir = Path(_path_map[company]["scorecards_dir"])
            _shortcomings_dir = Path(_path_map[company]["shortcomings_dir"])
            _scorecard_path = _scorecards_dir / f"job_{_safe_id}.scorecard.json"
            _shortcomings_path = _shortcomings_dir / f"job_{_safe_id}.shortcomings.json"
        else:
            _scorecard_path = None
            _shortcomings_path = None

        if not _has_providers:
            st.warning(
                "⚠️ **No LLM providers configured.**  \n"
                "Add at least one API key to `config.yaml → llm.providers` to enable scoring."
            )

        # ── Section A: Scorecard ──────────────────────────────────────
        st.markdown("#### 📋 Job Scorecard")

        _scorecard_exists = _scorecard_path and _scorecard_path.exists()
        _scorecard_data = None

        if _scorecard_exists:
            try:
                _scorecard_data = json.loads(_scorecard_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _scorecard_data = None

        if _scorecard_data:
            # Show formatted scorecard
            meta = _scorecard_data.get("meta", {})
            st.caption(
                f"**Role:** {meta.get('role', 'N/A')}  ·  "
                f"**Domains:** {', '.join(meta.get('domains', []))}  ·  "
                f"**Level:** {meta.get('seniority_level', 'N/A')}"
            )

            # Hard filters
            hard = _scorecard_data.get("hard_filters", {})
            if any(hard.values()):
                with st.expander("🚧 Hard Filters", expanded=False):
                    if hard.get("min_total_yoe"):
                        st.write(f"• Min YoE: **{hard['min_total_yoe']}**")
                    if hard.get("specific_credential"):
                        st.write(f"• Credentials: {', '.join(hard['specific_credential'])}")
                    if hard.get("regulatory_compliance"):
                        st.write(f"• Compliance: {', '.join(hard['regulatory_compliance'])}")

            # Pillars table
            pillars = _scorecard_data.get("pillars", {})
            if pillars:
                pillar_rows = []
                for pname, pdata in pillars.items():
                    pillar_rows.append({
                        "Pillar": pname,
                        "Weight": pdata.get("suggested_weight", 0),
                        "Required": ", ".join(pdata.get("req", [])),
                        "Equivalents": ", ".join(pdata.get("equiv", [])),
                    })
                st.dataframe(
                    pd.DataFrame(pillar_rows),
                    hide_index=True,
                    use_container_width=True,
                )

            # Editable JSON
            with st.expander("✏️ Edit Scorecard JSON", expanded=False):
                edited_json = st.text_area(
                    "Raw JSON — edit and save",
                    value=json.dumps(_scorecard_data, indent=2),
                    height=300,
                    key=f"scorecard_editor_{job_id}",
                )
                if st.button("💾 Save Edited Scorecard", key=f"save_sc_{job_id}"):
                    try:
                        parsed = json.loads(edited_json)
                        _scorecard_path.write_text(
                            json.dumps(parsed, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        st.success("✅ Scorecard saved.")
                    except json.JSONDecodeError as e:
                        st.error(f"❌ Invalid JSON: {e}")
        else:
            st.info("No scorecard generated yet for this job.")

        if _has_providers:
            btn_label = "🔄 Regenerate Scorecard" if _scorecard_data else "⚡ Generate Scorecard"
            if st.button(btn_label, key=f"gen_sc_{job_id}", type="primary" if not _scorecard_data else "secondary"):
                with st.spinner("Generating scorecard from job description..."):
                    jd = md_text
                    jd_clean = llm_scorer._strip_notes_section(jd) if jd else ""
                    if jd_clean.strip():
                        sc = llm_scorer.generate_scorecard(
                            jd_clean, _providers,
                            stage_params=_stage_params.get("scorecard"),
                        )
                        if sc:
                            _scorecards_dir.mkdir(parents=True, exist_ok=True)
                            _scorecard_path.write_text(
                                json.dumps(sc, indent=2, ensure_ascii=False),
                                encoding="utf-8",
                            )
                            st.success("✅ Scorecard generated!")
                            st.rerun()
                        else:
                            st.error("❌ Scorecard generation failed. Check terminal for details.")
                    else:
                        st.warning("Job description is empty.")

        st.divider()

        # ── Section B: Resume Evaluation ──────────────────────────────
        st.markdown("#### 📊 Resume Evaluation")

        _shortcomings_exists = _shortcomings_path and _shortcomings_path.exists()
        _shortcomings_data = None

        if _shortcomings_exists:
            try:
                _shortcomings_data = json.loads(_shortcomings_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _shortcomings_data = None

        if _shortcomings_data:
            sc_rel = _shortcomings_data.get("relevance", 0)
            sc_gaps = _shortcomings_data.get("shortcomings", [])
            sc_time = _shortcomings_data.get("evaluated_at", "")

            col_score, col_info = st.columns([1, 3])
            with col_score:
                st.metric("Relevance", f"{sc_rel}/100")
            with col_info:
                st.caption(f"Evaluated: {sc_time[:19] if sc_time else 'N/A'}")
                if sc_gaps:
                    st.markdown("**Shortcomings:**")
                    for gap in sc_gaps:
                        st.markdown(f"- {gap}")
                else:
                    st.success("No shortcomings identified — strong match!")
        else:
            st.info("Resume not yet evaluated against this job.")

        if _has_providers and _scorecard_data:
            if st.button("📊 Score My Resume", key=f"eval_{job_id}", type="primary" if not _shortcomings_data else "secondary"):
                with st.spinner("Evaluating resume against scorecard..."):
                    result = llm_scorer.score_job(
                        company, job_id, _llm_config,
                        scorecard_override=_scorecard_data,
                    )
                    if "error" in result:
                        st.error(f"❌ {result['error']}")
                    else:
                        st.success(f"✅ Score: {result['relevance']}/100")
                        st.cache_data.clear()
                        st.rerun()
        elif not _scorecard_data and _has_providers:
            st.caption("Generate a scorecard first to enable resume scoring.")

        st.divider()

        # ── Section C: LinkedIn Gap Check ─────────────────────────────
        st.markdown("#### 🔍 LinkedIn Gap Analysis")

        if _shortcomings_data and _shortcomings_data.get("shortcomings"):
            if _has_providers:
                if st.button("🔍 Check LinkedIn Gaps", key=f"gap_{job_id}"):
                    with st.spinner("Analyzing LinkedIn data against shortcomings..."):
                        linkedin_data = llm_scorer._read_linkedin_data(_llm_config)
                        gap_result = llm_scorer.check_linkedin_gaps(
                            _shortcomings_data["shortcomings"],
                            linkedin_data,
                            _providers,
                            stage_params=_stage_params.get("gap_analysis"),
                        )
                        if gap_result:
                            if gap_result.get("coverable"):
                                st.markdown(f"**✅ Coverable gaps ({len(gap_result['coverable'])}):**")
                                for item in gap_result["coverable"]:
                                    st.markdown(f"- **{item.get('gap', '')}**")
                                    st.caption(f"  Evidence: {item.get('evidence', '')} (from {item.get('source_file', '')})")
                            if gap_result.get("uncoverable"):
                                st.markdown(f"**❌ Uncoverable gaps ({len(gap_result['uncoverable'])}):**")
                                for gap in gap_result["uncoverable"]:
                                    st.markdown(f"- {gap}")
                        else:
                            st.error("Gap analysis failed.")
        elif _shortcomings_data and not _shortcomings_data.get("shortcomings"):
            st.success("No shortcomings to check — resume is a strong match!")
        else:
            st.caption("Score your resume first to enable LinkedIn gap analysis.")



# ---------------------------------------------------------------------------
# Generic editor renderer with open_modal action column
# ---------------------------------------------------------------------------

def _render_editor(
    source_df:   pd.DataFrame,
    display_cols: list[str],
    editor_key:  str,
    editable_cols: set[str],
) -> pd.DataFrame:
    """
    Prepend open_modal=False column, render st.data_editor, return the result.
    Non-editable columns have disabled=True set via _COL_CFG.
    """
    display = source_df[display_cols].copy().reset_index(drop=True)
    display["open_modal"] = False

    col_cfg = {k: v for k, v in _COL_CFG.items() if k in display.columns}
    # Force-disable columns not in editable set
    for col in display.columns:
        if col not in editable_cols and col != "open_modal" and col in col_cfg:
            pass  # disabled already set per column in _COL_CFG

    return st.data_editor(
        display,
        column_config=col_cfg,
        width="stretch",
        hide_index=True,
        num_rows="fixed",
        key=editor_key,
    )


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main() -> None:

    # ── Initialise ──────────────────────────────────────────────────────────

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

    # ── Base filter ─────────────────────────────────────────────────────────
    base = df[df["_company"].isin(company_filter)].copy()
    if search_text:
        mask = (
            base["title"].str.contains(search_text, case=False, na=False)
            | base["job_id"].str.contains(search_text, case=False, na=False)
        )
        base = base[mask]

    # Three-way split
    active_df   = _apply_sort(
        base[(base["visible"].str.lower() == "yes") & base["applied"].apply(_not_applied)],
        sort1, sort2,
    )
    sent_df     = _apply_sort(
        base[base["applied"].apply(_is_applied)],
        sort1, sort2,
    )
    archived_df = base[
        (base["visible"].str.lower() == "no") & base["applied"].apply(_not_applied)
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
    tab1, tab2, tab3 = st.tabs([
        f"🎯 Active Radar  ({len(active_df)})",
        f"📤 Sent Applications  ({len(sent_df)})",
        f"📦 Archived  ({len(archived_df)})",
    ])

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # TAB 1 — Active Radar
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab1:
        if active_df.empty:
            st.info("No active jobs match your filters. Try broadening your search.")
        else:
            t1_ver    = st.session_state.get("t1_ver", 0)
            t1_key    = f"tab1_editor_{t1_ver}"
            t1_source = active_df[_ACTIVE_BASE].copy().reset_index(drop=True)
            t1_source["open_modal"] = False

            # 1. INTERCEPT STATE BEFORE RENDERING
            modal_row = None
            if t1_key in st.session_state:
                edits = st.session_state[t1_key].get("edited_rows", {})
                for idx_str, changes in edits.items():
                    if changes.get("open_modal") is True:
                        modal_row = active_df.iloc[int(idx_str)]
                        t1_ver += 1
                        st.session_state["t1_ver"] = t1_ver
                        t1_key = f"tab1_editor_{t1_ver}"  # Force fresh editor key
                        break

            # 2. RENDER EDITOR
            edited = st.data_editor(
                t1_source,
                column_config={k: v for k, v in _COL_CFG.items() if k in t1_source.columns},
                width="stretch",
                hide_index=True,
                num_rows="fixed",
                key=t1_key,
            )

            # 3. TRIGGER MODAL OR PROCESS SAVES
            if modal_row is not None:
                show_job_modal(modal_row["job_id"], modal_row["title"], modal_row["_company"], modal_row["_md_dir"])
            else:
                # ── Write-back: applied_bool + relevance ──────────────────
                changes: list[dict] = []
                for idx in range(min(len(edited), len(t1_source))):
                    orig     = t1_source.iloc[idx]
                    edit     = edited.iloc[idx]
                    full_row = active_df.iloc[idx]

                    if bool(edit["applied_bool"]) != bool(orig["applied_bool"]):
                        new_val = (
                            datetime.date.today().isoformat()
                            if edit["applied_bool"] else ""
                        )
                        changes.append({
                            "job_id":   full_row["job_id"],
                            "csv_path": full_row["_csv_path"],
                            "field":    "applied",
                            "value":    new_val,
                        })

                    if int(edit["relevance"]) != int(orig["relevance"]):
                        changes.append({
                            "job_id":   full_row["job_id"],
                            "csv_path": full_row["_csv_path"],
                            "field":    "relevance",
                            "value":    int(edit["relevance"]),
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
            t2_ver    = st.session_state.get("t2_ver", 0)
            t2_key    = f"tab2_editor_{t2_ver}"
            t2_source = sent_df[_SENT_BASE].copy().reset_index(drop=True)
            t2_source["open_modal"] = False

            # 1. INTERCEPT STATE BEFORE RENDERING
            modal_row = None
            if t2_key in st.session_state:
                edits = st.session_state[t2_key].get("edited_rows", {})
                for idx_str, changes in edits.items():
                    if changes.get("open_modal") is True:
                        modal_row = sent_df.iloc[int(idx_str)]
                        t2_ver += 1
                        st.session_state["t2_ver"] = t2_ver
                        t2_key = f"tab2_editor_{t2_ver}"  # Force fresh editor key
                        break

            # 2. RENDER EDITOR
            edited_sent = st.data_editor(
                t2_source,
                column_config={k: v for k, v in _COL_CFG.items() if k in t2_source.columns},
                width="stretch",
                hide_index=True,
                num_rows="fixed",
                key=t2_key,
            )

            # 3. TRIGGER MODAL OR PROCESS SAVES
            if modal_row is not None:
                show_job_modal(modal_row["job_id"], modal_row["title"], modal_row["_company"], modal_row["_md_dir"])
            else:
                # Undo applied: applied_bool flipped from True → False
                changes: list[dict] = []
                for idx in range(min(len(edited_sent), len(t2_source))):
                    if not edited_sent.iloc[idx]["applied_bool"] and t2_source.iloc[idx]["applied_bool"]:
                        full_row = sent_df.iloc[idx]
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
            st.caption("Read-only. Click 📖 to view the cached Job Description.")
            t3_ver    = st.session_state.get("t3_ver", 0)
            t3_key    = f"tab3_editor_{t3_ver}"
            t3_source = archived_df[_ARCHIVED_BASE].copy().reset_index(drop=True)
            t3_source["open_modal"] = False

            # 1. INTERCEPT STATE BEFORE RENDERING
            modal_row = None
            if t3_key in st.session_state:
                edits = st.session_state[t3_key].get("edited_rows", {})
                for idx_str, changes in edits.items():
                    if changes.get("open_modal") is True:
                        modal_row = archived_df.iloc[int(idx_str)]
                        t3_ver += 1
                        st.session_state["t3_ver"] = t3_ver
                        t3_key = f"tab3_editor_{t3_ver}"  # Force fresh editor key
                        break

            # 2. RENDER EDITOR
            edited_arch = st.data_editor(
                t3_source,
                column_config={k: v for k, v in _COL_CFG.items() if k in t3_source.columns},
                disabled=_ARCHIVED_BASE,   # all content cols read-only; open_modal stays editable
                width="stretch",
                hide_index=True,
                num_rows="fixed",
                key=t3_key,
            )

            # 3. TRIGGER MODAL OR PROCESS SAVES
            if modal_row is not None:
                show_job_modal(modal_row["job_id"], modal_row["title"], modal_row["_company"], modal_row["_md_dir"])


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
