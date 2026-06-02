# 🎯 Core Job Tracker — Job Application OS

A self-hosted, multi-company job application CRM built entirely in Python. Scrapes live job listings from Workday's internal CXS API, tracks your application pipeline in structured CSV ledgers and rich Markdown files, and surfaces everything through a Streamlit "Job Application OS" dashboard — with an integrated LLM scoring pipeline that evaluates your resume against each job description.

---

## Features

| Layer | What it does |
|---|---|
| **Scraper** | Stateful `requests.Session` pagination against Workday's CXS API. Handles WAF cookie reuse, 130s timeouts, and inter-page delays. Parallel multi-company scraping via `ThreadPoolExecutor`. |
| **State Tracking** | 3-way reconciliation (New / Updated / Delisted). Self-healing `.md` file checks. "Late Write" CSV pattern populates authoritative HR posting dates. |
| **Rich Job Files** | Per-job `.md` files with metadata table (Posted Date, Age, Deadline), full HTML-to-Markdown job description via `html2text`, and an editable `## Notes` section. |
| **Web UI** | Streamlit 3-Tab CRM: Active Radar · Sent Applications · Archived. Inline applied date-stamping, relevance scoring, and `filelock` safe write-back. |
| **LLM Scorer** | On-demand scorecard generation + resume evaluation using free-tier cloud APIs (Groq, Gemini, Cerebras) or local models (Ollama/LM Studio). Provider cascade with smart rate-limit header awareness. |
| **Gap Analysis** | LinkedIn data export cross-referencing against identified shortcomings — shows what gaps your LinkedIn profile can cover before applying. |

---

## LLM Scoring Pipeline

The scorer uses a **two-pass architecture** (with an optional third pass):

```
Pass 1: Job Description → LLM → Scorecard JSON      (what does this job need?)
Pass 2: Resume + Scorecard → LLM → Score + Gaps      (how well do I match?)
Pass 3: Shortcomings + LinkedIn → LLM → Gap Report    (can I cover the gaps?)
```

Each pipeline stage defines an ordered list of preferred models in `config.yaml`. The client uses a **Model-First Cascade** to route requests.

**Scorecard** — The LLM extracts pillars (TECH, SYS, OPS, SEC, DOM, LDR, MSC, etc.) with relative weights that are auto-normalized to sum to 1000. A 1-shot JSON example in the prompt prevents schema collapse on smaller models. You can view and edit the scorecard in the UI before scoring.

**Shortcomings** — Specific, actionable gaps (not vague observations) are saved to disk and displayed in the UI. Old shortcomings never contaminate new LLM calls.

**Model-First Cascade** — Stages specify models in preference order (e.g., `llama-3.3-70b-versatile` → `gemini-2.0-flash`). For each model, the client finds all providers capable of serving it and round-robins through **all API keys** (family accounts) before advancing to the next model. `rpm_limit` is tracked **per key**, not per provider. Supports cloud APIs and local endpoints (Ollama, LM Studio via Tailscale).

---

## Prerequisites

- Python 3.11+
- Streamlit 1.35+ (Required for native `@st.dialog` and `vertical_alignment` features)
- Git Bash or any POSIX-compatible terminal (Windows)
- A Workday career site URL (to extract your `api_url` and filter facet IDs)
- At least one free-tier LLM API key (Groq, Gemini, Cerebras, etc.) — or a local Ollama/LM Studio endpoint

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/vishalgoel003/core-job-tracker.git
cd core-job-tracker

# 2. Create and activate a virtual environment
python -m venv venv
source venv/Scripts/activate        # Windows Git Bash
# or: venv\Scripts\activate.bat     # Windows CMD

# 3. Install all dependencies
pip install -r requirements.txt

# 4. Create your local config.yaml from the sample template
cp config.yaml.sample config.yaml          # Linux/macOS/Git Bash
# or: copy config.yaml.sample config.yaml   # Windows CMD/PowerShell

# 5. Add at least one LLM API key to config.yaml → llm.providers
# 6. Configure your company targets under companies:
```

---

## Project Structure

```
core-job-tracker/
├── config.yaml.sample       ← Tracked template (full company catalog + LLM config)
├── config.yaml              ← Local configuration (untracked)
├── AGENT.md                 ← Project rules and constraints
├── README.md
├── requirements.txt
├── .gitignore
├── src/
│   ├── __init__.py
│   ├── config_engine.py     ← Config loader + path resolver
│   ├── workday_scraper.py   ← Workday CXS API scraper + detail fetcher
│   ├── state_tracker.py     ← Reconciliation engine + parallel scraping
│   ├── llm_client.py        ← LLM API wrapper (cascade + rate-limit awareness)
│   └── llm_scorer.py        ← Scorecard generation + resume evaluation + gap check
├── web/
│   └── app.py               ← Streamlit Job Application OS (imports from src/)
├── targets/
│   └── [CompanyName]/
│       ├── master_jobs.csv      ← Canonical state ledger
│       ├── job_details/
│       │   └── job_<id>.md      ← Full JD + editable Notes
│       ├── scorecards/
│       │   └── job_<id>.scorecard.json   ← LLM-generated evaluation scheme
│       └── shortcomings/
│           └── job_<id>.shortcomings.json ← Resume gaps for this job
├── user_details/                ← Your profile data (untracked)
│   ├── resume.md
│   ├── extra.md
│   └── Basic_LinkedInDataExport/
└── reference/                   ← Read-only reference repos [SEC-3.1]
```

---

## Adding a Company

> All backend modules live in `src/` and use try/except relative imports, so they work both when imported as a package (`import src.config_engine`) and when run directly from the project root (`python src/state_tracker.py`).

Make sure you have created your local `config.yaml` by copying `config.yaml.sample`. Then, open `config.yaml` and add a new entry under `companies:`:


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

The scraper fetches all live jobs, reconciles them against the existing ledger, fetches full job descriptions for new listings, and writes everything to disk. Multiple companies are scraped in parallel.

```bash
# From the project root, with venv activated:
python src/state_tracker.py
```

**Expected output:**
```
=== state_tracker.py — Task 3: State Tracking & File Writer (v3) ===

  [PARALLEL] Scraping 2 companies with 2 workers
  Fetching live jobs for: Barclays
  Fetching live jobs for: Mastercard

  [Barclays] new=0  updated=230  delisted=0  healed=0
  [Mastercard] new=3  updated=45  delisted=1  healed=0

[DONE] Task 3 complete.
```

> **Self-healing:** If a `.md` file is missing or lacks a `## Job Description` section, the scraper automatically re-fetches and repairs it on the next run — without touching your `## Notes` content.

---

## LLM Scoring (CLI)

```bash
# Score all unscored (relevance=0) visible jobs across all companies:
python src/llm_scorer.py

# Score a specific job:
python src/llm_scorer.py --job JR-0000105811 --company Barclays

# Run LinkedIn gap analysis for a scored job:
python src/llm_scorer.py --gap-check JR-0000105811 --company Barclays
```

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
- `🤖 LLM / Resume` — Scorecard viewer/editor, resume scoring, LinkedIn gap analysis

---

## Quick Scraper Run (No UI Required)

```bash
# Run the scraper directly from the terminal:
python src/state_tracker.py
```

> The "🚀 Run Scraper" button in the Streamlit sidebar calls this same command via `subprocess.run`.

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
| `relevance` | integer 0–100 | Manual or LLM-assigned match score |
| `applied` | ISO date / `""` | Date you submitted an application, or blank |

---

## Supported ATS Platforms

| ATS Type | Extraction Mechanism | Status |
|---|---|---|
| **Workday** | Hidden JSON API (`POST` to CXS endpoint) | ✅ Supported |
| **Greenhouse** | Public REST API (`GET`) | 🔜 Roadmap |
| **Lever** | Public REST API (`GET`) | 🔜 Roadmap |
| **Eightfold.ai** | Internal GraphQL / POST | 🔜 Roadmap |
| **SmartRecruiters** | REST API | 🔜 Roadmap |
| **Oracle HCM** | REST Endpoints / HTML | 🔜 Roadmap |
| **SuccessFactors** | OData REST API (`GET`) | 🔜 Roadmap |

---

## Roadmap

- [x] Task 1: Config Engine
- [x] Task 2: Workday CXS Scraper
- [x] Task 3: State Tracking + Rich Markdown files
- [x] Task 4: Streamlit Job Application OS (3-Tab CRM)
- [x] Task 5: Restructured backend into `src/` package
- [x] Task 6: LLM Scoring Pipeline (Scorecard + Resume Eval + Gap Analysis)
- [x] Task 7: Parallel multi-company scraping
- [ ] Task 8: Multi-ATS support (Greenhouse, Lever, SmartRecruiters)
- [ ] Task 9: Automated daily scraper scheduler
- [ ] Task 10: Fully automated async scoring pipeline

---

## Technical Debt

- **Workday pagination hardcoded to 20** — The CXS API enforces max 20 per call. Currently handled by looping, but the offset logic should be centralized.
- **Console output interleaving** — Parallel scraping causes mixed output from multiple companies. Consider per-company log buffering.
- **Streamlit rerun hacks** — Version counters (`t1_ver`, `t2_ver`) force table refreshes after edits. May break on future Streamlit versions.

---

## Rules & Constraints

See [`AGENT.md`](AGENT.md) for the full project ruleset governing file isolation, network behaviour, terminal execution, and LLM integration.
