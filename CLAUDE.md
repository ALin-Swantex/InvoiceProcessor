# InvoiceProcessor

Internal invoice-processing app (FastAPI + vanilla JS UI + a background worker). Invoices arrive by Outlook or manual upload, PDFs are filed in SharePoint, fields are extracted by Claude, and Purchase Ledger / approvers move each invoice through review, PO matching, approval, payment and reconciliation. PostgreSQL holds the data; Microsoft Entra handles login. See `README.md`, `SOFTWARE_SPEC.md` and `INVOICE_WORKFLOW_FLOWCHART.md` for the business process.

## Working guidelines
These bias toward caution over speed; use judgment on trivial tasks.

**Think before coding.** State assumptions and ask when something is unclear. If there is more than one reasonable reading, present them instead of picking one silently. If a simpler approach exists, say so and push back.

**Keep it simple.** Write the minimum code that solves the request: no extra features, no abstraction for a single use, no configurability nobody asked for, no handling of impossible cases. If 200 lines could be 50, rewrite it.

**Make surgical changes.** Every changed line should trace to the request. Do not tidy adjacent code, comments or formatting, and match the existing style. Remove only the imports and code your own change made unused; mention other dead code instead of deleting it.

**Work toward a verifiable goal.** Turn the task into a check, then loop until it passes: a bug starts with a test that reproduces it, a refactor needs the tests green before and after. For multi-step work, state a short plan with a check for each step.

## Commands
```bash
python3 -m pytest -q                                   # full suite (mocked; no cloud, no .env needed)
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000   # web app (loads .env)
python3 -m app.outlook_worker                          # Outlook/SharePoint worker (one per mailbox)
python3 -m app.postgres_migrate                        # apply DB migrations
python3 -m app.demo                                    # sample data, local login, no cloud (never touches .env)
python3 -m scripts.check_live_azure [--extraction]     # read-only live checks; --extraction makes one small paid AI call
docker compose up -d --build                           # web + worker; `--profile tools run --rm migrate`; `--profile demo up demo`
```

## Layout
- `app/main.py` - `create_app(...)` wiring only. Routes are in `app/routers/` (auth, invoices, statements, reference, activity, admin, outlook, ui); shared helpers in `app/deps.py`; request models in `app/schemas.py`.
- `app/static/` - the whole UI: `index.html`, `app.css`, `app.js` (no build step, no frameworks).
- `app/invoice_lifecycle.py` - every workflow stage (extraction, review, PO matching, Sage, approvals, payment, reconciliation).
- `app/ai_extraction.py`, `app/extraction_checks.py` - Claude extraction (JSON-schema output, optional escalation to a stronger model) plus deterministic checks.
- `app/outlook_worker.py`, `app/outlook_graph.py`, `app/sharepoint*.py`, `app/graph_http.py` - intake; `graph_http.py` is the shared Graph client (retries, pagination).
- `app/postgres_*.py` + `app/postgres_migrations/` - production stores and schema. SQLite twins (`invoices.py`, `companies.py`, ...) exist for tests and demo mode.
- `app/demo.py` - demo mode. `tests/` - pytest suite.

## Rules that are easy to break
- **Two backends.** `create_app()` with injected stores uses SQLite (tests, demo); with none it builds PostgreSQL stores from the environment. The Postgres SQL is not covered by the unit tests: verify changes to `postgres_*.py` against a real local Postgres (throwaway Docker container or a scratch database), then remove it.
- **Migrations 001-017 are immutable** (checksummed; editing one breaks every deployed database). Add new ones as `018_...sql`. Apply the migration before deploying code that needs it.
- **Config comes from the environment.** `.env` is loaded only by entrypoints through `load_project_environment()`, never at import time. Tests set `INVOICE_PROCESSOR_SKIP_DOTENV=1` and clear cloud variables in `tests/conftest.py`. The Graph credentials are `GRAPH_*` (old `OUTLOOK_MCP_*` names still work as deprecated fallbacks, see `app/environment.py`).
- **UI tests assert exact strings.** `tests/test_api.py::test_invoice_review_interface_contains_required_sections` checks element ids, `data-tab` values, headings and JS snippets across `index.html`, `app.css` and `app.js`. Keep those intact when editing the UI; change an assertion only when the markup genuinely moved.
- **IRJ numbers** are per-company sequences, saved on the invoice at allocation so a failed step does not leak one. **Email stage keys** (for example `po_matching`, `approval_reminder_{level}_{due_date}`) must not change: they stop duplicate emails. Duplicate detection compares all statuses, including cancelled.
- **The worker acts on real systems.** Pointed at real SharePoint and a fresh database it registers and moves every PDF in Incoming. Do not start it casually.
- **Local password login is development only** (`AUTH_LOCAL_LOGIN_ENABLED`); its placeholder accounts live in `app/auth.py` (`SEED_USERS`). Never enable it on a reachable deployment.

## Conventions
- Never commit `.env`, secrets, screenshots or other generated output. Keep scratch scripts outside the repo.
- Any change to extraction behaviour needs a test using the fake client in `tests/test_ai_extraction.py`; do not call the live API from tests.
- Run the whole suite before committing, and also with `.env` moved aside (it must pass either way).
- Git: do not add `Co-Authored-By` trailers or "Generated with Claude Code" lines to commits or pull request descriptions.
