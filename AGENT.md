# Agent Behavioral Rules & Workspace Constraints

## Technical Stack
- Runtime: Native Python 3.14.0 inside the local virtual environment (`venv`).
- Engine: Pure HTTP networking via the `requests` library.
- Strict Rule: No Playwright, Puppeteer, or browser UI simulation is allowed. 

## Architectural Reference Check
- You have functional open-source code inside `./reference/ats-scrapers` and `./reference/ever-jobs`. 
- Analyze those repositories to understand how to format endpoint URLs and headers for Workday, Greenhouse, and Lever APIs before writing code.

## Step-by-Step Execution Protocol
1. Read `project_description.md` as your execution roadmap.
2. You must operate on ONE specific task at a time.
3. Before writing, modifying, or deleting code, explain your implementation approach and explicitly ask the user for confirmation.
4. DEFINITION OF DONE: A task is not complete until you run the code natively in the local terminal, verify it connects successfully to a live API endpoint, and confirm files are correctly generated on disk.
5. If a request throws an error, output the raw HTTP status code to the user interface. Never assume success.

## Security & Write Isolation
- CRITICAL: The `./reference/` directory is strictly READ-ONLY. You are forbidden from creating, modifying, or deleting files inside `./reference/ever-jobs` or `./reference/ats-scrapers`.
- All code generation must be strictly bound to the root directory (`run_scrapers.py`, `dashboard.py`) and the automatically generated `./targets/` output path.

## Loop Interruption Rules
- You must NOT use automated loop execution routines (`/goal`) for writing network functions.
- If an HTTP request returns a status code other than 200, or if a JSON parsing error occurs, you must immediately halt, dump the raw response headers to the panel, and invoke the `/grill-me` routine to ask the user for manual guidance.
- Do not attempt to automatically fix connection logic more than once without human text sign-off.

## Execution Environment Bounds
- Terminal Type: Git Bash on Windows 11.
- Virtual Environment Activation Path: `source venv/Scripts/activate` (Do not use `bin/activate`).
- Python Target: Explicitly execute modules via `python run_scrapers.py` within the active activated terminal sub-shell environment.