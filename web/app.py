"""
web/app.py — Job Application OS
---------------------------------
Streamlit-based CRM front-end for the core-job-tracker pipeline.

Runs from the project root as:
    streamlit run web/app.py

Architecture:
  - Reads all master_jobs.csv files via config_engine (ATS-agnostic, multi-tenant).
  - 3-Tab CRM: Active Radar / Sent Applications / Archived.
  - Inline `applied` date-stamp + `relevance` score editing via st.data_editor.
  - filelock safe write-back prevents race conditions with the background scraper.
  - @st.dialog with 3 tabs: Job Description / My Notes (live .md edit) / LLM placeholder.
"""

import sys
from pathlib import Path

# Path injection — web/app.py imports from the project root where config_engine lives
sys.path.insert(0, str(Path(__file__).parent.parent))

import datetime
import re
from collections import defaultdict

import filelock
import pandas as pd
import streamlit as st

import config_engine

# ---------------------------------------------------------------------------
# Page configuration
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Job Application OS",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS — subtle polish without a full design framework
st.markdown("""
<style>
    /* Metric cards */
    [data-testid="stMetric"] {
        background: #1a1a2e;
        border: 1px solid #2d2d44;
        border-radius: 10px;
        padding: 16px 20px;
    }
    /* Tab labels */
    .stTabs [data-baseweb="tab"] {
        font-size: 14px;
        font-weight: 600;
        padding: 8px 20px;
    }
    /* Tighten data editor rows */
    [data-testid="stDataEditor"] { border-radius: 8px; }
    /* Sidebar header */
    [data-testid="stSidebar"] h2 { color: #a78bfa; }
    /* Caption colour */
    .stCaption { color: #888; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOCK_TIMEOUT_S = 5   # seconds to wait for filelock before warning user

SORT_OPTIONS = ["Deadline ↑", "Relevance ↓", "Posted Date ↓", "Company A→Z"]
_SORT_MAP = {
    "Deadline ↑":    ("last_date",             True),
    "Relevance ↓":   ("relevance",             False),
    "Posted Date ↓": ("first_discovered_on",   False),
    "Company A→Z":   ("_company",              True),
}

# Columns shown in Tab 1 editor
_ACTIVE_COLS   = ["applied_bool", "relevance", "_company", "job_id", "title",
                  "first_discovered_on", "last_date"]

# Columns shown in Tab 2 (sent) read-only table
_SENT_COLS     = ["applied", "_company", "job_id", "title",
                  "first_discovered_on", "last_date", "relevance"]

# Columns shown in Tab 3 (archived) read-only table
_ARCHIVED_COLS = ["_company", "job_id", "title", "first_discovered_on", "last_date"]

# ---------------------------------------------------------------------------
# Markdown notes helpers
# ---------------------------------------------------------------------------

# Matches the Notes block: from "## Notes\n\n" up to (but not including)
# the next "\n\n---" separator or end-of-file. Lookahead preserves separator.
_NOTES_RE = re.compile(r"(## Notes\n\n)(.*?)(?=\n\n---|$)", re.DOTALL)


def _read_notes(md_text: str) -> str:
    """Extract user notes from the ## Notes section. Returns '' for the placeholder comment."""
    m = _NOTES_RE.search(md_text)
    if not m:
        return ""
    body = m.group(2).strip()
    return "" if body.startswith("<!--") else body


def _write_notes(md_text: str, new_notes: str) -> str:
    """Non-destructively replace the ## Notes content. Preserves all other sections."""
    def _rep(m: re.Match) -> str:
        return f"## Notes\n\n{new_notes.strip()}"

    result = _NOTES_RE.sub(_rep, md_text, count=1)
    if result == md_text:
        # Notes section absent — append it
        result = md_text.rstrip() + f"\n\n## Notes\n\n{new_notes.strip()}\n"
    return result


# ---------------------------------------------------------------------------
# Applied field helpers (backward-compatible with legacy "yes"/"no" values)
# ---------------------------------------------------------------------------

def _not_applied(val: str) -> bool:
    """Returns True if the applied field means 'not yet applied'."""
    return val.strip().lower() in ("", "no")


def _is_applied(val: str) -> bool:
    return not _not_applied(val)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30)
def load_all_jobs() -> pd.DataFrame:
    """
    Concatenate master_jobs.csv from every configured company directory.
    Injects three internal columns (_company, _csv_path, _md_dir) used by
    write-back and modal — these are never displayed in the UI.
    Cache TTL=30s so a background scraper run is reflected within half a minute.
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

    # Type coercions
    combined["relevance"] = (
        pd.to_numeric(combined["relevance"], errors="coerce").fillna(0).astype(int)
    )
    combined["first_discovered_on"] = pd.to_datetime(
        combined["first_discovered_on"], errors="coerce"
    )
    combined["last_date"] = pd.to_datetime(combined["last_date"], errors="coerce")

    # Derived checkbox column — backward-compatible with legacy "yes"/"no"
    combined["applied_bool"] = combined["applied"].apply(_is_applied)

    return combined


# ---------------------------------------------------------------------------
# Write-back
# ---------------------------------------------------------------------------

def _write_back(changes: list[dict]) -> None:
    """
    Apply a list of field changes to their per-company CSVs using filelock.

    Each change dict: { job_id, csv_path, field, value }

    Groups changes by csv_path to minimise file opens.
    On lock timeout (scraper is writing), shows a warning and aborts without crashing.
    On success, clears the @st.cache_data and triggers a rerun.
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
    """
    Multi-tiered Pandas sort. na_position='last' ensures rows with no deadline
    (NaT) always sink to the bottom, making urgent deadlines float to the top.
    """
    col1, asc1 = _SORT_MAP[sort1]
    col2, asc2 = _SORT_MAP[sort2]
    return (
        df.sort_values(by=[col1, col2], ascending=[asc1, asc2], na_position="last")
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# Job Details modal
# ---------------------------------------------------------------------------

@st.dialog("Job Details", width="large")
def show_job_modal(job_id: str, title: str, company: str, md_dir: str) -> None:
    """
    Three-tab modal:
      Tab 1 — Job Description: renders the full .md file content.
      Tab 2 — My Notes: text_area that reads/writes the ## Notes section only.
      Tab 3 — LLM / Resume: placeholder UI for the generative pipeline.
    """
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

    # --- Tab 1: Job Description ---
    with tab_jd:
        if md_text:
            st.markdown(md_text, unsafe_allow_html=False)
        else:
            st.warning(
                "No detail file found on disk.  \n"
                "Run `python state_tracker.py` to generate `.md` files."
            )

    # --- Tab 2: My Notes ---
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

        if st.button("💾 Save Notes", type="primary", use_container_width=True):
            if not md_path.exists():
                st.error("Cannot save — the detail file does not exist yet.")
            else:
                updated = _write_notes(md_text, new_notes)
                md_path.write_text(updated, encoding="utf-8")
                st.success("✅ Notes saved to disk.")

    # --- Tab 3: LLM / Resume placeholder ---
    with tab_llm:
        st.info(
            "🔮 **LLM pipeline — coming in the next phase.**  \n"
            "This tab will invoke your local Ollama model against your master profile "
            "and this specific JD to produce tailored LaTeX assets."
        )
        st.divider()
        c1, c2 = st.columns(2)
        c1.button(
            "⚡ Generate Tailored Resume",
            disabled=True,
            use_container_width=True,
            help="Will call local Ollama API: profile.md + JD → LaTeX resume",
        )
        c2.button(
            "✉️  Generate Cover Letter",
            disabled=True,
            use_container_width=True,
            help="Will call local Ollama API: profile.md + JD → cover letter",
        )
        st.caption("Generated assets will appear here and be saved to `targets/[company]/generated/`.")


# ---------------------------------------------------------------------------
# Shared column config (disabled = read-only in data_editor / display in dataframe)
# ---------------------------------------------------------------------------

_COL_CFG = {
    "_company":            st.column_config.TextColumn("Company",    disabled=True, width="small"),
    "job_id":              st.column_config.TextColumn("Job ID",     disabled=True, width="medium"),
    "title":               st.column_config.TextColumn("Title",      disabled=True, width="large"),
    "first_discovered_on": st.column_config.DateColumn("Posted",     disabled=True, width="small"),
    "last_date":           st.column_config.DateColumn("Deadline",   disabled=True, width="small"),
    "relevance":           st.column_config.NumberColumn("Score",    min_value=0, max_value=100, step=1, width="small"),
    "applied":             st.column_config.TextColumn("Applied On", disabled=True, width="small"),
    "visible":             st.column_config.TextColumn("Status",     disabled=True, width="small"),
    "applied_bool":        st.column_config.CheckboxColumn(
                               "✅ Applied",
                               help="Check to mark applied. Today's date is saved to the CSV.",
                               width="small",
                           ),
}


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
            "Run `python state_tracker.py` first to populate the tracker, "
            "then refresh this page."
        )
        st.stop()

    all_companies = sorted(df["_company"].unique().tolist())

    # ── Sidebar controls ────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("## ⚙️ Controls")

        company_filter = st.multiselect(
            "Company", options=all_companies, default=all_companies,
            help="Show jobs from selected companies only",
        )
        search_text = st.text_input(
            "🔍 Search", placeholder="Title, Job ID, keyword...",
        )

        st.divider()
        st.markdown("**Sort Priority**")
        sort1 = st.selectbox("Primary sort",   SORT_OPTIONS, index=0)
        sort2 = st.selectbox("Secondary sort", SORT_OPTIONS, index=1)

        st.divider()
        deadline_window = st.slider(
            "⏰ Deadline alert window (days)", min_value=1, max_value=30, value=7,
        )

        st.divider()
        if st.button("🔄 Refresh Now", use_container_width=True,
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

    # ── Funnel Metrics ribbon ───────────────────────────────────────────────
    today          = pd.Timestamp.today().normalize()
    deadlines_soon = int(
        (
            base["last_date"].notna()
            & ((base["last_date"] - today).dt.days.between(0, deadline_window))
        ).sum()
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🎯 Active Radar",       len(active_df))
    m2.metric("📤 Sent",               len(sent_df))
    m3.metric(
        f"⏰ Deadlines ≤{deadline_window}d",
        deadlines_soon,
        delta="urgent" if deadlines_soon else None,
        delta_color="inverse" if deadlines_soon else "off",
    )
    m4.metric("🏢 Companies", len(company_filter))

    st.divider()

    # ── 3 Main Tabs ─────────────────────────────────────────────────────────
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
            st.info(
                "No active jobs match your current filters.  \n"
                "Try broadening your search or running the scraper."
            )
        else:
            active_display = active_df[_ACTIVE_COLS].reset_index(drop=True)

            edited = st.data_editor(
                active_display,
                column_config={k: v for k, v in _COL_CFG.items() if k in _ACTIVE_COLS},
                use_container_width=True,
                hide_index=True,
                key="tab1_editor",
                num_rows="fixed",
            )

            # ── Delta detection & filelock write-back ──────────────────────
            changes: list[dict] = []
            for idx in range(min(len(edited), len(active_display))):
                original = active_display.iloc[idx]
                edit     = edited.iloc[idx]
                full_row = active_df.iloc[idx]   # has _csv_path, job_id

                # applied_bool toggled
                if bool(edit["applied_bool"]) != bool(original["applied_bool"]):
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

                # relevance score edited
                if int(edit["relevance"]) != int(original["relevance"]):
                    changes.append({
                        "job_id":   full_row["job_id"],
                        "csv_path": full_row["_csv_path"],
                        "field":    "relevance",
                        "value":    int(edit["relevance"]),
                    })

            if changes:
                _write_back(changes)   # clears cache + st.rerun() on success

            # ── Job Details opener ─────────────────────────────────────────
            st.divider()
            sel_col, btn_col = st.columns([5, 1])
            with sel_col:
                title_map   = dict(zip(active_df["job_id"], active_df["title"]))
                selected_id = st.selectbox(
                    "Select job",
                    options=active_df["job_id"].tolist(),
                    format_func=lambda jid: f"{jid}  —  {title_map.get(jid, '')}",
                    label_visibility="collapsed",
                    key="tab1_select",
                )
            with btn_col:
                if st.button("📖 Open Details", use_container_width=True, key="tab1_open"):
                    row = active_df[active_df["job_id"] == selected_id].iloc[0]
                    show_job_modal(
                        row["job_id"], row["title"], row["_company"], row["_md_dir"]
                    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # TAB 2 — Sent Applications
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab2:
        if sent_df.empty:
            st.info(
                "No applications sent yet.  \n"
                "Tick the **✅ Applied** checkbox in the Active Radar tab to start tracking."
            )
        else:
            st.caption("Click any row to open Job Details.")
            event = st.dataframe(
                sent_df[_SENT_COLS].reset_index(drop=True),
                column_config={k: v for k, v in _COL_CFG.items() if k in _SENT_COLS},
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="tab2_table",
            )
            sel_rows = getattr(getattr(event, "selection", None), "rows", [])
            if sel_rows:
                row = sent_df.iloc[sel_rows[0]]
                show_job_modal(row["job_id"], row["title"], row["_company"], row["_md_dir"])

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # TAB 3 — Archived
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab3:
        if archived_df.empty:
            st.info(
                "No archived jobs yet.  \n"
                "Jobs appear here when they are removed from the live company career site "
                "on the next scraper run (`visible = no`)."
            )
        else:
            st.caption(
                "Read-only. These postings are no longer active on the company career site. "
                "Click any row to view the cached Job Description."
            )
            event = st.dataframe(
                archived_df[_ARCHIVED_COLS].reset_index(drop=True),
                column_config={k: v for k, v in _COL_CFG.items() if k in _ARCHIVED_COLS},
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="tab3_table",
            )
            sel_rows = getattr(getattr(event, "selection", None), "rows", [])
            if sel_rows:
                row = archived_df.iloc[sel_rows[0]]
                show_job_modal(row["job_id"], row["title"], row["_company"], row["_md_dir"])


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
