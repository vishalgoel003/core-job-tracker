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
        border-radius : 10px;
        padding       : 6px 12px !important;
    }

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
    body = m.group(2).strip()
    return "" if body.startswith("<!--") else body


def _write_notes(md_text: str, new_notes: str) -> str:
    def _rep(m: re.Match) -> str:
        return f"## Notes\n\n{new_notes.strip()}"
    result = _NOTES_RE.sub(_rep, md_text, count=1)
    if result == md_text:
        result = md_text.rstrip() + f"\n\n## Notes\n\n{new_notes.strip()}\n"
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
            return

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
    "title":               st.column_config.TextColumn("Title",    disabled=True, width="large"),
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
        new_notes = st.text_area(
            "Your notes — saved directly to the `.md` file",
            value=current_notes,
            height=340,
            placeholder=(
                "Interview prep notes...\n"
                "Recruiter contact: ...\n"
                "Salary range: ...\n"
                "Key skills to highlight: ..."
            ),
        )
        if st.button("💾 Save Notes", type="primary", width="stretch"):
            if not md_path.exists():
                st.error("Cannot save — detail file does not exist yet.")
            else:
                updated = _write_notes(md_text, new_notes)
                md_path.write_text(updated, encoding="utf-8")
                st.success("✅ Notes saved to disk.")

    with tab_llm:
        st.info(
            "🔮 **LLM pipeline — coming in the next phase.**  \n"
            "This tab will invoke your local Ollama model against your master profile "
            "and this specific JD to produce tailored LaTeX assets."
        )
        st.divider()
        c1, c2 = st.columns(2)
        c1.button("⚡ Generate Tailored Resume", disabled=True, width="stretch",
                  help="Will call local Ollama API: profile.md + JD → LaTeX resume")
        c2.button("✉️  Generate Cover Letter",   disabled=True, width="stretch",
                  help="Will call local Ollama API: profile.md + JD → cover letter")
        st.caption("Generated assets will appear here and be saved to `targets/[company]/generated/`.")


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

    # ── Header ──────────────────────────────────────────────────────────────
    st.markdown("# 🎯 Job Application OS")
    st.caption("Multi-company tracker · Powered by the Workday CXS scraper pipeline")

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
                    subprocess.run(
                        [sys.executable, "src/state_tracker.py"],
                        capture_output=True,
                        text=True,
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

    # ── Funnel Metrics ───────────────────────────────────────────────────────
    today          = pd.Timestamp.today().normalize()
    deadlines_soon = int(
        (
            base["last_date"].notna()
            & ((base["last_date"] - today).dt.days.between(0, deadline_window))
        ).sum()
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🎯 Active Radar",      len(active_df))
    m2.metric("📤 Sent",              len(sent_df))
    m3.metric(
        f"⏰ Deadlines ≤{deadline_window}d",
        deadlines_soon,
        delta="urgent" if deadlines_soon else None,
        delta_color="inverse" if deadlines_soon else "off",
    )
    m4.metric("🏢 Companies", len(company_filter))

    st.divider()

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

            edited = st.data_editor(
                t1_source,
                column_config={k: v for k, v in _COL_CFG.items() if k in t1_source.columns},
                width="stretch",
                hide_index=True,
                num_rows="fixed",
                key=t1_key,
            )

            # ── Modal trigger (checked FIRST — if fired, skip write-back) ──
            for idx in range(len(edited)):
                if edited.iloc[idx]["open_modal"]:
                    row = active_df.iloc[idx]
                    # Increment version → key changes → checkbox resets next rerun
                    st.session_state["t1_ver"] = t1_ver + 1
                    show_job_modal(row["job_id"], row["title"], row["_company"], row["_md_dir"])
                    break   # only one modal per render cycle
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

            edited_sent = st.data_editor(
                t2_source,
                column_config={k: v for k, v in _COL_CFG.items() if k in t2_source.columns},
                width="stretch",
                hide_index=True,
                num_rows="fixed",
                key=t2_key,
            )

            # Modal trigger (for-else: break if modal fired, else do write-back)
            for idx in range(len(edited_sent)):
                if edited_sent.iloc[idx]["open_modal"]:
                    row = sent_df.iloc[idx]
                    st.session_state["t2_ver"] = t2_ver + 1
                    show_job_modal(row["job_id"], row["title"], row["_company"], row["_md_dir"])
                    break
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

            edited_arch = st.data_editor(
                t3_source,
                column_config={k: v for k, v in _COL_CFG.items() if k in t3_source.columns},
                disabled=_ARCHIVED_BASE,   # all content cols read-only; open_modal stays editable
                width="stretch",
                hide_index=True,
                num_rows="fixed",
                key=t3_key,
            )

            for idx in range(len(edited_arch)):
                if edited_arch.iloc[idx]["open_modal"]:
                    row = archived_df.iloc[idx]
                    st.session_state["t3_ver"] = t3_ver + 1
                    show_job_modal(row["job_id"], row["title"], row["_company"], row["_md_dir"])
                    break


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
