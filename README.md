# Invoice Processor

Internal invoice-processing application built with FastAPI and Microsoft 365.
It receives invoice files from Outlook or manual upload, stores PDFs in
SharePoint, extracts invoice data with Azure Document Intelligence, and manages
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
- Azure PostgreSQL stores invoices, companies, suppliers, approval routes,
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

See [INVOICE_WORKFLOW_FLOWCHART.md](INVOICE_WORKFLOW_FLOWCHART.md) for the
complete state flow.

## Requirements

- Python 3.11 or newer
- Access to the configured Microsoft 365 tenant and SharePoint site
- Azure Database for PostgreSQL for the connected deployment
- LibreOffice when Outlook Excel attachments must be converted to PDF

## Initial setup

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[azure,test]'
cp .env.example .env
```

Configure `.env` with the required Entra, Microsoft Graph, SharePoint, Azure
PostgreSQL, and Document Intelligence settings. Never commit `.env` or secrets.

The application runtime uses PostgreSQL for invoices, configuration, sessions,
activity, IRJ sequences, and the Outlook processing queue:

```dotenv
INVOICE_STORE_BACKEND=postgres
CONFIG_STORE_BACKEND=postgres
AUTH_LOCAL_LOGIN_ENABLED=false
AUTH_COOKIE_SECURE=true
```

## Database migrations

Apply migrations after initial setup and whenever new migrations are pulled:

```bash
python3 -m app.postgres_migrate
```

The deployment identity applying migrations requires schema-change permission.
The runtime identity should use the narrower grants documented in
`app/postgres_runtime_grants.sql.template`.

Invoice source, extraction, review, approval, payment, reconciliation, queue,
session, and configuration metadata are stored in PostgreSQL. The canonical PDF
remains in SharePoint. For read-only Power BI reporting, see
[POWER_BI_INTEGRATION.md](POWER_BI_INTEGRATION.md).

## Run the application

After installing the project, the easiest cross-platform option is:

```bash
invoice-processor
```

This opens a small desktop launcher on Windows, macOS, and Linux. Its **Start**
button starts both the web application and Outlook worker, **Open web app**
opens the browser, and closing it stops both processes. For a server without a
desktop environment, use `invoice-processor --headless --open-browser`.

The launcher is installed by the existing setup command:

```bash
python3 -m pip install -e '.[azure,test]'
```

You can still run the services separately when diagnosing an individual
component:

Run the web application and worker in separate terminals from the repository
root.

**Terminal 1 — web application**

```bash
source .venv/bin/activate
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**Terminal 2 — Outlook and SharePoint worker**

```bash
source .venv/bin/activate
python3 -m app.outlook_worker
```

Open <http://127.0.0.1:8000>.

Do not run multiple worker instances against the same mailbox unless deployment
coordination explicitly supports it.

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
source .venv/bin/activate
pytest -q
```

The suite uses local and mocked services; passing tests do not establish live
Azure connectivity. Run `python -m scripts.check_live_azure` for read-only
PostgreSQL, Outlook, SharePoint, and Document Intelligence smoke checks.

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

## Reference documents

- [SOFTWARE_SPEC.md](SOFTWARE_SPEC.md) — current requirements, architecture,
  delivery status, and remaining work
- [GENERAL_PROCESS.md](GENERAL_PROCESS.md) — current business process and
  operational rules
- [INVOICE_WORKFLOW_FLOWCHART.md](INVOICE_WORKFLOW_FLOWCHART.md) — current
  workflow and implementation mapping
- [OUTLOOK_MCP_SETUP.md](OUTLOOK_MCP_SETUP.md) — Microsoft Graph mailbox and
  webhook configuration
- [POWER_BI_INTEGRATION.md](POWER_BI_INTEGRATION.md) — reporting views and
  Power BI connection instructions
- [USER_ACCEPTANCE_TEST_CASES.md](USER_ACCEPTANCE_TEST_CASES.md) — pilot test
  checklist and release criteria
