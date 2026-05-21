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