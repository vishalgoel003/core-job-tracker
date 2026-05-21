# Product Specification: Lean Corporate Job Tracker

## 1. Objective
A lightweight Python command-line utility that fetches corporate job listings directly from target company ATS backend APIs using pre-filtered configuration rules. It handles state tracking via local CSV ledgers and flat Markdown files.

## 2. Storage Directory Architecture
The execution engine must automatically read from a base configuration and output structures inside isolated folders per employer:

```

targets/
└── [employer_name]/
    ├── master_jobs.csv       <-- State tracking ledger
    └── job_details/          <-- Text descriptions folder
        ├── job_12345.md
        └── job_67890.md

```

### Schema Protocol:
- **master_jobs.csv**: Must contain the exact columns: `job_id,title,first_discovered_on,last_date,visible,relevance,applied`
- **job_<id>.md**: Contains the text description of the job specifications.

## 3. Incremental Tasks Roadmap

- **Task 1: Configuration Engine**: Write a Python module to parse `config.yaml` and dynamically generate the target output directory paths (`targets/[company_name]/job_details/`) if they do not exist.
- **Task 2: Workday API Scraper Engine**: Implement the network execution loop for Workday endpoints. The engine must accept search payloads, process pagination via `limit` and `offset` configurations, and return un-truncated arrays of active listings.
- **Task 3: State Tracking & File Writer Module**: Implement the deduplication routine. If a `job_id` is new: append to the CSV ledger and create the corresponding detail Markdown file. If it already exists: update `visible=yes`. If a tracked job is missing from the active pull: mark `visible=no`.
- **Task 4: Aggregated Operational Dashboard**: Build a standalone script (`dashboard.py`) that reads across all employer directories and prints a clean, chronological text overview of all opportunities sorted by discovery date.