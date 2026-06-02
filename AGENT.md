# Agent Behavioral Rules & Workspace Constraints

## 1. Technical Stack Constraints
- **[TECH-1.1]** Runtime Environment: Must execute inside native Python 3.14.0 within the local virtual environment (`venv`).
- **[TECH-1.2]** Networking Wrapper: Pure synchronous HTTP requests via the native `requests` library.
- **[TECH-1.3]** Browser Simulation Ban: Use of Playwright, Puppeteer, Selenium, or browser UI engines is strictly forbidden.
- **[TECH-1.4]** Authorized Dependencies: You are explicitly permitted to use production-grade parsing and formatting packages (`pyyaml`, `html2text`) to ensure data integrity and clean Markdown conversion.
- **[TECH-1.5]** LLM Integration: HTTP calls to LLM endpoints (cloud free-tier or local Ollama/LM Studio) via `requests` are authorized. No Python SDK dependencies.

## 2. Stateful Networking & Firewall Avoidance
- **[NET-2.1]** Stateful Requests: All corporate endpoint scraper modules must utilize `requests.Session()` rather than stateless independent HTTP requests to preserve tracking and security cookies across deep pages.
- **[NET-2.2]** Browser Headers: Every HTTP request dictionary must pass a standard, realistic web browser `User-Agent` string to safely avoid corporate edge firewall 403 blocks.
- **[NET-2.3]** Non-200 Error Handling: On any non-200 HTTP response code, you must immediately halt, output the raw response status and header dict to the terminal, and invoke the `/grill-me` protocol. Never attempt an automated retry loop.
- **[NET-2.4]** LLM Rate-Limit Awareness: All LLM calls must parse and respect `Retry-After` and `X-RateLimit-*` response headers. On 429/quota-exceeded, cascade to next provider. Store observed limits for future automation.

## 3. Directory Security & Write Isolation
- **[SEC-3.1]** Reference Folders Read-Only: The `./reference/` path is strictly read-only. You are permanently forbidden from adding, modifying, or deleting code inside reference subfolders.
- **[SEC-3.2]** Scope Isolation: All generated application assets must be strictly bound to the project root directory and the dynamically generated `./targets/` output folders.
- **[SEC-3.3]** Secrets Handling: API keys live exclusively in `config.yaml` (gitignored). Never hardcode keys in source files or commit them.

## 4. Sequential Execution Protocol
- **[EXEC-4.1]** Task Isolation: You must operate on exactly one task from `project_description.md` at a time.
- **[EXEC-4.2]** Pre-Flight Confirmation: Before generating, updating, or deleting any codebase files, you must explain your technical blueprint and explicitly await the user's text verification.
- **[EXEC-4.3]** Definition of Done: A task is not complete until you natively execute the code in the workspace terminal, verify successful integration, and confirm expected files are present on disk.
- **[EXEC-4.4]** On-Demand LLM Only: LLM scoring/evaluation calls are never auto-triggered during scraping. They require explicit user action (CLI command or UI button).