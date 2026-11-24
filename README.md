# Invoice Processor

Internal invoice-processing application built with FastAPI and Microsoft 365.
It receives invoice files from Outlook or manual upload, stores PDFs in
SharePoint, extracts invoice data with an AI model (Claude), and manages
review, PO matching, approval, payment, reconciliation, and audit history.

## Current workflow

```text
Outlook or manual upload
  → SharePoint Incoming
  → AI extraction and Purchase Ledger review
  → duplicate check and IRJ assignment
  → PO matching or nominal Sage registration
  → Approver 1 and optional Approver 2
  → payment routing
  → payment
  → bank reconciliation
  → complete
```

Important behavior:

- SharePoint is the canonical PDF store.
- PostgreSQL stores invoices, companies, suppliers, approval routes,
  payment settings, audit events, IRJ sequences, and email-stage claims.
- Microsoft Entra provides login and application roles.
- Queries and approval holds do not move an invoice or change its workflow
  stage.
- Each configured notification stage sends at most one successful email per
  invoice, including across query/resume loops.
- IRJ search shows the current status, notes, and chronological audit trail.
- Purchase Ledger review shows the PDF beside editable extracted fields,
  highlights low confidence values, records human corrections, and explains
  the selected workflow route.
- Documents requiring review are moved from Incoming to the shared SharePoint
  `Invoices/Flagged Invoices` folder until resolved.
- The Admin panel shows the Outlook worker heartbeat and queue, supports retry
  of failed queue items, and highlights approval routes with missing emails.
- Timestamps are displayed in UK time.

## Requirements

- Python 3.11 or newer (3.12 is used in the Docker image)
- PostgreSQL 16: a local server for development and testing, or Azure Database
  for PostgreSQL for a connected deployment
- An Anthropic API key (`ANTHROPIC_API_KEY`) for invoice extraction
- For live intake: access to the Microsoft 365 tenant, the shared mailbox and
  the SharePoint site
- Excel attachments are converted to PDF through Microsoft Graph, so no local
  LibreOffice is needed

## Installation

### 1. Get the code and install it

macOS or Linux:

```bash
git clone https://github.com/ALin-Swantex/InvoiceProcessor.git
cd InvoiceProcessor
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[azure,test]'
```

Windows (PowerShell):

```powershell
git clone https://github.com/ALin-Swantex/InvoiceProcessor.git
cd InvoiceProcessor
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install -e ".[azure,test]"
```

The `azure` extra installs the PostgreSQL driver as well as the Azure sign-in
libraries, so it is needed even with a local database.

### 2. Create a PostgreSQL database

For a local database (macOS with Homebrew shown; on Windows use the
installer from postgresql.org):

```bash
brew install postgresql@16
brew services start postgresql@16
createdb invoice_processing_dev
```

### 3. Configure `.env`

```bash
cp .env.example .env
```

Fill in the values. The essentials are:

```dotenv
INVOICE_STORE_BACKEND=postgres
CONFIG_STORE_BACKEND=postgres
DATABASE_URL=postgresql://USER:PASSWORD@127.0.0.1:5432/invoice_processing_dev?sslmode=disable
ANTHROPIC_API_KEY=...
```

plus the `GRAPH_*` and `SHAREPOINT_*` settings for the mailbox and SharePoint.
`.env.example` explains every setting. For an Azure database, use the Azure
`DATABASE_URL` with `sslmode=verify-full` instead. Never commit `.env` or
secrets.

For a local, plain-HTTP setup you can sign in with the development accounts in
`app/auth.py` by setting `AUTH_LOCAL_LOGIN_ENABLED=true` and
`AUTH_COOKIE_SECURE=false`. Never enable local login on a deployment others can
reach; there, use Microsoft Entra sign-in with `AUTH_COOKIE_SECURE=true`.

### 4. Apply the database migrations

```bash
python3 -m app.postgres_migrate
```

Run this again whenever new migrations are pulled (the launcher below also
runs it on every start). The identity applying migrations needs schema-change
permission; the runtime identity should use the narrower grants in
`app/postgres_runtime_grants.sql.template`.

### 5. Check the setup

```bash
python3 -m pytest -q                          # mocked; needs no cloud access
python3 -m scripts.check_live_azure           # read-only live checks
python3 -m scripts.check_live_azure --extraction   # plus one small paid AI call
```

## Running the application

### One click: the launcher

```bash
python3 start.py
```

On Windows you can also double-click `start.py`. On macOS you can make a
double-click Desktop app that opens the launcher without a Terminal window
(adjust the two paths):

```bash
osacompile -o ~/Desktop/"Invoice Processor.app" -e 'do shell script "cd /path/to/InvoiceProcessor && /path/to/python3 -m app.launcher > /dev/null 2>&1 &"'
```

One **Start** click applies the database migrations, then starts the web
application and the Outlook worker. **Open web app** opens
<http://127.0.0.1:8000> once the web app answers. Each service shows its status, and all output appears in one
colour-coded log that is also saved to `runtime_data/launcher.log`. **Stop**,
or closing the window, stops everything.

**The worker acts on the real mailbox and SharePoint**, so Start processes
real files. Pointed at a fresh database it registers and moves every PDF in
SharePoint Incoming.

Without a desktop (a server, or a terminal session):

```bash
python3 start.py --headless --open-browser   # Ctrl+C stops everything
```

### Services one at a time

For diagnosing one component, run each in its own terminal from the
repository root, with the virtual environment active:

```bash
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000   # web app
python3 -m app.outlook_worker                                  # worker
```

Run exactly one worker per mailbox.

## Demo mode (no Azure needed)

To look around the application, or to work on the UI while Azure is
unavailable, run it on temporary local data with sample invoices in every
workflow stage:

```bash
python -m app.demo            # http://localhost:8000, Ctrl+C to stop
docker compose -f docker/docker-compose.yml --profile demo up demo   # http://localhost:8001
```

It prints the demo accounts and passwords on start-up. Demo mode uses local
SQLite storage and local password sign-in only. It ignores `.env` and every
Azure, Microsoft 365, SharePoint and PostgreSQL setting, so it can never touch
real systems, and its data is discarded when it stops (`--data-dir` keeps it).

## Run with Docker

The application is containerised: one image (Python 3.12, non-root, working
files on a `/data` volume) runs every service, and `docker/docker-compose.yml` starts
the web application and the Outlook worker with your settings from `.env`
(never copied into the image). It needs Docker Desktop or Docker Engine with
Compose. The Docker files live in `docker/`; run these commands from the
repository root.

```bash
cp .env.example .env                              # then fill in the values
docker compose -f docker/docker-compose.yml --profile tools run --rm migrate   # apply migrations
docker compose -f docker/docker-compose.yml up -d --build                      # web app + worker
docker compose -f docker/docker-compose.yml logs -f web worker                 # follow the logs
docker compose -f docker/docker-compose.yml down                               # stop
```

Open <http://localhost:8000>. Notes:

- **The database is not in a container.** Compose expects PostgreSQL to run
  elsewhere. Inside a container `127.0.0.1` is the container itself, so for a
  PostgreSQL server on the same machine use `host.docker.internal` as the host
  in `DATABASE_URL` (Docker Desktop on macOS and Windows). For Azure PostgreSQL
  the containers must be able to resolve and reach the server (VPN, private
  network or an allowed IP).
- The containers make outbound HTTPS calls to Microsoft Graph and the
  Anthropic API.
- Run exactly one `worker` per mailbox; do not scale it.
- Working files live on the `invoice-data` volume (`/data`). SharePoint stays
  the canonical PDF store, so the volume can be rebuilt.
- The web container reports health through `/api/health`, which includes the
  worker heartbeat.
- Behind HTTPS set `AUTH_COOKIE_SECURE=true` and a public `APP_PUBLIC_URL`; use
  `false` only for plain-HTTP local testing.
- The desktop launcher is not used with Docker; Compose does its job.

## Health check

```bash
curl http://127.0.0.1:8000/api/health
```

Expected response:

```json
{"status":"ok","mode":"outlook-intake"}
```

## Tests

```bash
python3 -m pytest -q
```

The suite uses local and mocked services; passing tests do not establish live
Azure connectivity. Run `python -m scripts.check_live_azure` for read-only
PostgreSQL, Outlook, and SharePoint smoke checks. Add `--extraction` to also
make one small paid AI extraction call on a generated sample invoice.

## AI invoice extraction

Invoice PDFs are read by Claude, which returns the supplier, customer, invoice
number, PO, date, total and currency with a quoted piece of evidence and a
certainty for each. Deterministic checks then verify the values against the
PDF's text (is the invoice number really in the document?), the arithmetic (net
plus tax equals the total), the date and the configured company names. Anything
uncertain or failing a check goes to Needs Review, and the Admin confidence
threshold applies as before. The model and prompt version are recorded with each
extraction.

Set `ANTHROPIC_API_KEY`. `EXTRACTION_MODEL` (default `claude-opus-5-5`) reads
every invoice. Optionally set `EXTRACTION_ESCALATION_MODEL` (for example Sonnet
as the first model and Opus as the escalation model): when the first model has
difficulty (a required field missing, uncertain or failing a check, or it cannot
read the document), the stronger model reads the invoice again and its answer is
used. If the first model was confident about a field and the second reads it
differently, the invoice goes to Needs Review. Escalated invoices cost two model
calls, and the recorded model shows both (`first -> second`). Tune with
`EXTRACTION_EFFORT` and `EXTRACTION_ESCALATION_EFFORT`. With no key the application does
not guess: invoices wait in Needs Review. Invoice PDFs are sent to the Anthropic
API (first and last page only for invoices over three pages), so confirm your
data-handling terms before enabling it.

## Authentication and roles

Users and role assignments are managed in Microsoft Entra under the Invoice
Processor Enterprise Application. Assign exactly one supported app role:

| Entra app-role value | Application role |
|---|---|
| `InvoiceProcessor.Admin` | Admin |
| `InvoiceProcessor.PurchaseLedger` | Purchase Ledger |
| `InvoiceProcessor.Approver1` | Approver 1 |
| `InvoiceProcessor.Approver2` | Approver 2 |
| `InvoiceProcessor.Purchasing` | Purchasing |

The application does not maintain a users table. Its local authentication
database contains only session and OAuth-flow state. Local password login is an
explicit development/test option only.

## Supplier import

Admins can bulk-import supplier master data for a selected company. A workbook
can include supplier account details, payment terms, bank account, approver
names, and the columns `Approver Email` and `Approver 2 Email`.

Approval email addresses must be verified. The application does not guess
addresses, and Sage registration stops in **Needs Review** if a required
approval recipient has no email.

## Operations

- Run the web application and worker under a persistent process manager in
  production.
- Monitor both process logs and `/api/health`.
- Store credentials in deployment secrets or Azure Key Vault.
- Back up PostgreSQL according to the required retention policy.
- Apply migrations before deploying application code that uses new tables.
- Use a public HTTPS endpoint for Microsoft Graph webhooks; local polling can be
  enabled for development. Local polling starts at midnight today in UK time by
  default, and PostgreSQL prevents a restart from queueing the same message
  twice.

Webhook subscription commands:

```bash
python3 -m app.outlook_subscription create
python3 -m app.outlook_subscription renew "$OUTLOOK_SUBSCRIPTION_ID"
```
