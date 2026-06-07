# 🎯 Core Job Tracker — Job Application OS

A self-hosted, multi-company job application CRM built entirely in Python. Scrapes live job listings from Workday's internal CXS API, tracks your application pipeline in structured CSV ledgers and rich Markdown files, and surfaces everything through a Streamlit "Job Application OS" dashboard — with an integrated LLM scoring pipeline that evaluates your resume against each job description.

---

## Features

| Layer | What it does |
|---|---|
| **Scraper** | Stateful `requests.Session` pagination against Workday's CXS API. Handles WAF cookie reuse, 130s timeouts, and inter-page delays. Parallel multi-company scraping via `ThreadPoolExecutor`. |
| **State Tracking** | 3-way reconciliation (New / Updated / Delisted). Self-healing `.md` file checks. "Late Write" CSV pattern populates authoritative HR posting dates. |
| **Rich Job Files** | Per-job `.md` files with metadata table (Posted Date, Age, Deadline), full HTML-to-Markdown job description via `html2text`, and an editable `## Notes` section. |
| **Web UI** | Streamlit 6-Tab CRM: Active Radar · Sent · Archive & Skipped · Manual Entry · Insights · Settings. Inline applied date-stamping, relevance scoring, and `filelock` safe write-back. |
| **LLM Scorer** | On-demand scorecard generation + resume evaluation using free-tier cloud APIs (Groq, Gemini, Cerebras) or local models (Ollama/LM Studio). Provider cascade with smart rate-limit header awareness. |
| **Gap Analysis** | Supplementary data cross-referencing against identified shortcomings — shows what gaps your Supplementary data can cover before applying. |

---

## LLM Scoring Pipeline

The scorer uses a **two-pass architecture** (with an optional third pass):

```
Pass 1: Job Description → LLM → Scorecard JSON      (what does this job need?)
Pass 2: Resume + Scorecard → LLM → Score + Gaps      (how well do I match?)
Pass 3: Shortcomings + Supplementary Data → LLM → Gap Report    (can I cover the gaps?)
```

Each pipeline stage defines an ordered list of preferred models in `config.yaml`. The client uses a **Model-First Cascade** to route requests.

**Scorecard** — The LLM extracts pillars (TECH, SYS, OPS, SEC, DOM, LDR, MSC, etc.) with relative weights that are auto-normalized to sum to 1000. A 1-shot JSON example in the prompt prevents schema collapse on smaller models. You can view and edit the scorecard in the UI before scoring.

**Shortcomings** — Specific, actionable gaps (not vague observations) are saved to disk and displayed in the UI. Old shortcomings never contaminate new LLM calls.

**Model-First Cascade** — Stages specify models in preference order (e.g., `openai/gpt-oss-120b` → `gemini-3.5-flash`). For each model, the client finds all providers capable of serving it and round-robins through **all API keys** (family accounts) before advancing to the next model. `rpm_limit` is tracked **per key**, not per provider. The same model available on multiple providers (e.g., `gpt-oss-120b` on Groq, Cerebras, OpenRouter) should be listed with each provider's model ID for automatic cross-provider failover. Supports cloud APIs and local endpoints (Ollama, LM Studio via Tailscale).

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

## Preparing Your Profile Data

Before scoring jobs, you must populate your `user_details/` folder:

1. **Resume (`resume.md`)**: The pipeline expects your resume in Markdown format. If you have a PDF, copy its text into ChatGPT/Claude and ask: *"Format this resume into clean Markdown"*. Save it as `user_details/resume.md`.
2. **Supplementary Data (LinkedIn Export)**: To run the Gap Fill Analysis, the system needs your LinkedIn profile data.
   - Go to LinkedIn → Settings & Privacy → Data Privacy → **Get a copy of your data**.
   - Request the **Profile** data bundle (or larger).
   - Once emailed to you, extract the CSV files (`Skills.csv`, `Positions.csv`, `Projects.csv`, etc.) into `user_details/SupplementaryData/`.
3. **Custom Notes (`custom_notes.md`)**: An optional markdown file where you can manually type out extra certifications, GitHub repos, or skills not captured in your LinkedIn dump.

---

## Troubleshooting & Debugging

If you encounter issues with the MapReduce Gap Fill pipeline (e.g., the LLM is aggressively dropping skills or hallucinating arrays), you can turn on deep logging to inspect the exact input/output payloads being sent to the LLM.

```bash
# In Git Bash (or Linux/Mac), run Streamlit with this flag:
DEBUG_INSIGHTS_LOG=1 streamlit run web/app.py
```

This will create `logs/insight_pipeline_debug.log` containing the complete prompts and raw JSON responses. Use this to verify if the LLM is disobeying prompt instructions. 

**Note on Local Imports:**
The codebase has been refactored to strictly avoid local `import` statements within functions. If modifying `app.py` or `llm_scorer.py`, keep all imports at the global file level to prevent Python `UnboundLocalError`.

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
├── deploy/                      ← Docker configuration & OCI packaging scripts
│   └── docker-compose.prod.yml  ← Native cloud deployment config
├── logs/                        ← Internal rotated logs (scraper.log, scorer.log)
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
│   ├── Manual/                  ← Special isolated tracking for non-automated jobs
│   └── [CompanyName]/
│       ├── master_jobs.csv      ← Canonical state ledger
│       ├── job_details/
│       │   └── job_<id>.md      ← Full JD + editable Notes
│       ├── scorecards/
│       │   └── job_<id>.scorecard.json   ← LLM-generated evaluation scheme
│       └── shortcomings/
│           ├── job_<id>.shortcomings.json ← Resume gaps for this job
│           └── job_<id>.gap_analysis.json ← Gap analysis results
├── user_details/                ← Your profile data (untracked)
│   ├── resume.md
│   ├── custom_notes.md
│   └── SupplementaryData/
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

# To permanently prune dead jobs (visible=no & never applied to) from disk/ledgers:
python src/state_tracker.py --prune
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

# Run Gap analysis for a scored job:
python src/llm_scorer.py --gap-check JR-0000105811 --company Barclays
```

### LLM Pipeline Stages
The application uses 5 strictly decoupled LLM pipeline stages, configured in `config.yaml` to allow independent model routing and parameter tuning:
1. **`scorecard`**: Extracts JD structure into a 7-pillar evaluation scheme. (Traditional Pipeline)
2. **`evaluation`**: Scores the user's resume against the generated scorecard. (Traditional Pipeline)
3. **`gap_analysis`**: Evaluates a single job's shortcomings against user's Supplementary Data. (Traditional Pipeline)
4. **`global_insights`**: Map-Reduce chunking and clustering of all shortcomings across all jobs. (Insights Pipeline)
5. **`global_gap_fill`**: Final mapping of massive global skill clusters against Supplementary Data. (Insights Pipeline)

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
| **🎯 Active Radar** | All visible jobs not yet applied to | ✅ App · 🚫 Skip · 🤖 Score · 🗑️ Delete (Manual) |
| **📤 Sent Applications** | All jobs with an application date | Read-only · Click 🔗 to open details |
| **📦 Archive & Skipped** | Delisted jobs (dead) and Skipped jobs (live) | Uncheck 🚫 to restore Skipped to Active Radar |
| **➕ Manual Entry** | Form to add external non-Workday jobs | Create custom jobs isolated from the scraper |
| **🧠 Insights & Growth** | MapReduce pipeline of aggregated missing skills | View Quick Wins · View Historical Misses · Run Gap Fill |
| **⚙️ Settings & Files** | Cloud File Manager for Docker deployments | Edit `config.yaml`, `resume.md`, and `SupplementaryData` without SSH |

**Sidebar controls:** Company filter, keyword search, sort priority (Deadline ↑ / Relevance ↓ / Posted Date ↓), deadline alert window.
- **🚀 Run Scraper:** Fetches latest jobs via a detached background subprocess. Safe to close the browser tab. Logs are written to `logs/scraper.log`.
- **🧹 Prune Dead Jobs:** Permanently deletes files and CSV rows for unapplied jobs in the Archived/Skipped state.

**Job Details Page (click 🔗 View on any row):**
Opens in a new browser tab via URL routing (fully compatible with Cloudflare Tunnels).
- `📄 Job Description` — Full rendered Markdown from the `.md` file
- `✏️ My Notes` — Edit and save directly to the `## Notes` section of the `.md` file
- `🤖 LLM / Resume` — Express Pipeline (1-click evaluation with smart hashing/caching), scorecard editor, resume scoring, and Gap analysis

---

## Quick Scraper Run (No UI Required)

```bash
# Run the scraper directly from the terminal:
python src/state_tracker.py
```

> The "🚀 Run Scraper" button in the Streamlit sidebar calls this same command detached via `subprocess.Popen`.

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
| `applied` | ISO date / `""` | Date you submitted an application, or blank (`"no"` in legacy files) |
| `skipped` | `"yes"` / `""` | Whether you marked this job as unsuitable |

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

## Docker Deployment (Local & OCI)

The tracker can be fully Dockerized for both local testing and remote OCI deployment. The Docker setup lives in the `deploy/` folder and allows you to map your locally fetched data as external volumes, preventing data center IPs from getting banned by Workday's WAF.

### Workflow A: Local Testing
Builds the image from source and maps your local data into the container:
```bash
cd deploy
docker compose up --build -d
```
Access the app at `http://localhost:8501`. Any changes made in the UI will write back to your local `targets/` folder.

### Workflow B: Production OCI Deployment (Native Build)
Because cross-compiling Python packages for ARM64 on a Windows machine can be extremely slow and error-prone, the best approach is to package the source code and build the image natively on your OCI server. You don't even need a Docker Hub account!

1. **Package Everything (Source + Data):**
```bash
bash deploy/package.sh
```
This generates a `release.zip` in your project root containing your application source code, Docker configs, and your local state data (`targets/`, `config.yaml`).

2. **Deploy and Build:**
`scp release.zip` to your OCI Ubuntu instance, unzip it, and run the generated script:
```bash
bash unpack_and_run.sh
```
The script will natively build the ARM64 image using your OCI server's hardware (which is incredibly fast and reliable) and launch the application.

---

## Roadmap

- [x] Task 1: Config Engine
- [x] Task 2: Workday CXS Scraper
- [x] Task 3: State Tracking + Rich Markdown files
- [x] Task 4: Streamlit Job Application OS (3-Tab CRM)
- [x] Task 5: Restructured backend into `src/` package
- [x] Task 6: LLM Scoring Pipeline (Scorecard + Resume Eval + Gap Analysis)
- [x] Task 7: Parallel multi-company scraping
- [x] Task 8: LLM `--debug` matrix mode — force-test all models × providers × stages to generate a success-rate matrix. Implement JSON-mode fallback extractor to handle strict-mode 400 errors from providers like Groq.
- [x] Task 9: Native Dockerization & OCI remote packaging strategy
- [x] Task 10: Manual Jobs Pipeline (Isolated UI-driven tracking for non-Workday external jobs)
- [x] Task 11: Automated daily scraper scheduler
- [ ] Task 12: Multi-ATS support (Greenhouse, Lever, SmartRecruiters)
- [ ] Task 13: Fully automated async scoring pipeline
- [ ] Task 14: Phase 2 API Migration (FastAPI backend + Next.js frontend)

---

## Technical Debt

- **Workday pagination hardcoded to 20** — The CXS API enforces max 20 per call. Currently handled by looping, but the offset logic should be centralized.
- **Console output interleaving** — Parallel scraping causes mixed output from multiple companies. Consider per-company log buffering.

---

## Rules & Constraints

See [`AGENT.md`](AGENT.md) for the full project ruleset governing file isolation, network behaviour, terminal execution, and LLM integration.
