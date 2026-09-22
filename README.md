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

Production should use:

```dotenv
INVOICE_STORE_BACKEND=postgres
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

## Run the application

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

The current suite contains 181 passing tests.

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
  enabled for development.

Webhook subscription commands:

```bash
python3 -m app.outlook_subscription create
python3 -m app.outlook_subscription renew "$OUTLOOK_SUBSCRIPTION_ID"
```

## Reference documents

- [SOFTWARE_SPEC.md](SOFTWARE_SPEC.md) — original requirements and decisions
- [GENERAL_PROCESS.md](GENERAL_PROCESS.md) — detailed process and business rules
- [INVOICE_WORKFLOW_FLOWCHART.md](INVOICE_WORKFLOW_FLOWCHART.md) — current
  workflow and implementation mapping
- [OUTLOOK_MCP_SETUP.md](OUTLOOK_MCP_SETUP.md) — Microsoft Graph mailbox and
  webhook configuration
