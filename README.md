# 🎯 Core Job Tracker — Job Application OS

A self-hosted, multi-company job application CRM built entirely in Python. Scrapes live job listings from Workday's internal CXS API, tracks your application pipeline in structured CSV ledgers and rich Markdown files, and surfaces everything through a Streamlit "Job Application OS" dashboard — with a future LLM resume-generation pipeline pre-wired.

---

## Features

| Layer | What it does |
|---|---|
| **Scraper** | Stateful `requests.Session` pagination against Workday's CXS API. Handles WAF cookie reuse, 130s timeouts, and inter-page delays. |
| **State Tracking** | 3-way reconciliation (New / Updated / Delisted). Self-healing `.md` file checks. "Late Write" CSV pattern populates authoritative HR posting dates. |
| **Rich Job Files** | Per-job `.md` files with metadata table (Posted Date, Age, Deadline), full HTML-to-Markdown job description via `html2text`, and an editable `## Notes` section. |
| **Web UI** | Streamlit 3-Tab CRM: Active Radar · Sent Applications · Archived. Inline applied date-stamping, relevance scoring, and `filelock` safe write-back. |
| **LLM Pipeline** | Placeholder tabs pre-wired for local Ollama resume/cover letter generation (coming next phase). |

---

## Prerequisites

- Python 3.11+
- Git Bash or any POSIX-compatible terminal (Windows)
- A Workday career site URL (to extract your `api_url` and filter facet IDs)

---

## Installation

```bash
# 1. Clone the repository
git clone <your-repo-url>
cd core-job-tracker

# 2. Create and activate a virtual environment
python -m venv venv
source venv/Scripts/activate        # Windows Git Bash
# or: venv\Scripts\activate.bat     # Windows CMD

# 3. Install all dependencies
pip install -r requirements.txt
```

---

## Project Structure

```
core-job-tracker/
├── config.yaml              ← Company configuration (ATS URLs, search filters)
├── config_engine.py         ← Config loader + path resolver
├── workday_scraper.py       ← Workday CXS API scraper + detail fetcher
├── state_tracker.py         ← Reconciliation engine + Markdown/CSV writer
├── dashboard.py             ← CLI dashboard (terminal, no dependencies)
├── web/
│   └── app.py               ← Streamlit Job Application OS
├── targets/
│   └── [CompanyName]/
│       ├── master_jobs.csv      ← Canonical state ledger
│       └── job_details/
│           └── job_<id>.md      ← Full JD + editable Notes
├── requirements.txt
├── AGENT.md                 ← Project rules and constraints
└── README.md
```

---

## Adding a Company

Open `config.yaml` and add a new entry under `companies:`:

```yaml
companies:
  - name: "YourCompany"           # Used as the directory name under targets/
    ats_type: "workday"
    api_url: "https://yourcompany.wd5.myworkdayjobs.com/wday/cxs/yourcompany/Careers/jobs"
    payload:
      searchText: "Java"          # Your keyword filter
      limit: 20
      offset: 0
      appliedFacets:
        locations:
          - "<location_facet_id>"    # Extract from the Workday URL query string
        jobFamilyGroup:
          - "<family_group_id>"      # Extract from the Workday URL query string
```

**How to find facet IDs:**
1. Open the company's Workday career site in your browser.
2. Apply your desired location and job family filters.
3. Open DevTools → Network → filter for `jobs` — the POST payload contains the facet IDs.

---

## Running the Scraper

The scraper fetches all live jobs, reconciles them against the existing ledger, fetches full job descriptions for new listings, and writes everything to disk.

```bash
# From the project root, with venv activated:
python state_tracker.py
```

**Expected output:**
```
=== state_tracker.py — Task 3: State Tracking & File Writer (v3) ===

  Fetching live jobs for: Barclays
  [Barclays] new=12  updated=218  delisted=0  healed=0
  [Barclays] Detail fetches : 12 ok / 0 skipped
  [Barclays] CSV rows       : 230  → targets\Barclays\master_jobs.csv
  [Barclays] Detail .md     : 230  → targets\Barclays\job_details

[DONE] Task 3 complete.
```

> **Self-healing:** If a `.md` file is missing or lacks a `## Job Description` section, the scraper automatically re-fetches and repairs it on the next run — without touching your `## Notes` content.

---

## Launching the Web UI

```bash
# From the project root, with venv activated:
streamlit run web/app.py
```

Open **http://localhost:8501** in your browser.

### UI Overview

| Tab | Contents | Editable |
|---|---|---|
| **🎯 Active Radar** | All visible jobs not yet applied to | ✅ Applied checkbox (saves today's date) · Score (0–100) |
| **📤 Sent Applications** | All jobs with an application date | Read-only · Click row to open details |
| **📦 Archived** | Delisted jobs (removed from live board) | Read-only · Click row to view cached JD |

**Sidebar controls:** Company filter, keyword search, sort priority (Deadline ↑ / Relevance ↓ / Posted Date ↓), deadline alert window.

**Job Details modal (click any row or use the selector):**
- `📄 Job Description` — Full rendered Markdown from the `.md` file
- `✏️ My Notes` — Edit and save directly to the `## Notes` section of the `.md` file
- `🤖 LLM / Resume` — Placeholder for local Ollama integration (next phase)

---

## CLI Dashboard (No UI Required)

```bash
python dashboard.py                     # Full chronological view
python dashboard.py --deadlines         # Urgent deadlines only
python dashboard.py --company Barclays  # Single company
python dashboard.py --days 30           # 30-day deadline window
```

---

## CSV Schema

`targets/[CompanyName]/master_jobs.csv`

| Column | Type | Description |
|---|---|---|
| `job_id` | string | Workday requisition ID (e.g. `JR-0000105811`) |
| `title` | string | Job title |
| `first_discovered_on` | ISO date | Authoritative HR posting date (from `startDate`) |
| `last_date` | ISO date | Application deadline (from `endDate`); blank if open-ended |
| `visible` | `yes` / `no` | Whether the job is currently live on the career site |
| `relevance` | integer 0–100 | Your manual or LLM-assigned match score |
| `applied` | ISO date / `""` | Date you submitted an application, or blank |

---

## Roadmap

- [x] Task 1: Config Engine
- [x] Task 2: Workday CXS Scraper
- [x] Task 3: State Tracking + Rich Markdown files
- [x] Task 4: Aggregated CLI Dashboard
- [x] Task 5: Streamlit Job Application OS
- [ ] Task 6: Local LLM Resume Generator (Ollama + LaTeX)
- [ ] Task 7: Multi-ATS support (Greenhouse, Lever, SmartRecruiters)
- [ ] Task 8: Automated daily scraper scheduler

---

## Rules & Constraints

See [`AGENT.md`](AGENT.md) for the full project ruleset governing file isolation, network behaviour, terminal execution, and LLM integration.
