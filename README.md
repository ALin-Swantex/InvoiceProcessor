# Outlook Invoice Intake

This project connects the first stage of the invoice workflow:

1. Microsoft Graph sends a webhook when an email reaches the invoice Inbox.
2. The web application queues the Outlook message ID.
3. A worker reads each attachment, converts XLS/XLSX to PDF when necessary,
   and uploads the PDF to `Invoices/Incoming Invoices` in SharePoint.
4. The same worker monitors that SharePoint folder. Each new DriveItem is
   registered exactly once, including PDFs placed there outside Outlook.
5. When configured, the worker sends a processing-cache copy to Azure AI
   Document Intelligence using the Invoice MCP Entra identity.
6. Workflow tables let users expand an invoice in place to review its
   SharePoint-backed PDF, extracted fields, confidence values, and warnings.

The production SharePoint site and document library are connected through
Microsoft Graph. Azure AI Document Intelligence and PostgreSQL adapters are
implemented but require their Azure resource settings. Sage and outbound email
delivery are not connected yet.

For local testing without a public HTTPS webhook, set
`OUTLOOK_LOCAL_POLLING_ENABLED=true`. The worker will poll unread messages
through the same direct Microsoft Graph connection. Production should continue
to use Graph webhooks.

## Implemented interface

- Received-invoice selector populated from the configured invoice database.
- Real PDF preview cached from the canonical SharePoint DriveItem.
- Real source email sender, subject, and received time.
- Company, supplier, invoice number, PO number, invoice date, and route.
- Net amount, VAT, total, currency, payment terms, and due date.
- Azure's `prebuilt-invoice` fields populate the review form when
  `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` is configured.
- Company, supplier, supplier invoice number, invoice date, total, and currency
  are critical extraction fields; missing or low-confidence values are sent to
  Purchase Ledger review.
- Purchase Ledger confirmation routes the canonical PDF through the configured
  SharePoint company structure.
- Workflow rows expand in place at review, Sage, approval, payment, and completed
  stages to show the PDF beside extracted and processing details.
- Responsive desktop and mobile layout.

Routing details are intentionally not displayed in the interface.

## Implemented backend logic

`POST /api/workflow/confirm` accepts the confirmed invoice fields and returns the
action plan required by the integration layer:

- A confirmed PO number routes the invoice to the configured PO-matching folder,
  sets `Awaiting PO Matching`, and requires a Purchase Ledger notification.
- No PO number routes the invoice to the company folder and prefixes the PDF
  filename with the IRJ.
- Required destinations and recipients are validated before a decision is
  returned.
- The lifecycle service executes SharePoint moves and blocks the corresponding
  state transition if a configured SharePoint move fails.

## SharePoint structure

The connected site is
`https://swantex0.sharepoint.com/sites/InvoiceProcessing`, using its
`Documents` library. Shared intake and rejection folders are:

- `Invoices/Incoming Invoices`
- `Invoices/Rejected Invoices`

The admin panel discovers complete company roots directly under `Invoices`.
The currently discovered roots are `CEL`, `GBCC`, `GIFTED`, `LING`, `PK`, and
`SWAN`. A root is selectable only when all of these relative paths exist:

```text
Nominal Invoices/
  Approver 1/
  Approver 2/
  On hold/
PO Invoices/
  PO Match/
  On hold/
Approved for payment/
  BACS/
  BANKLINE/
  FOREIGN POA/
Paid/
Reconciled/
```

Company configuration stores the selected root and derives every workflow
destination from it. The application never creates or renames this structure.

## IRJ numbering

IRJ numbering is configured per company in the Admin panel:

- `SWAN` and `CEL` default to **Manual at Sage**. Purchase Ledger must enter
  the six-digit IRJ during Sage registration.
- Other companies default to **Automatic**. Each company has an independent
  sequence.
- For an automatic company, enter the latest IRJ already used on paper. The
  first digitally allocated number is the following number.
- IRJs must be unique within a company, but the same six-digit IRJ can exist
  for different companies.

Run `python -m app.outlook_worker` alongside the web application. The worker
handles both queued Outlook messages and SharePoint Incoming monitoring.
`SHAREPOINT_INCOMING_POLL_SECONDS` controls the scan interval. Local files under
`SHAREPOINT_INVOICE_CACHE_DIR` are disposable processing/preview caches; the
DriveItem ID is the durable intake identity and SharePoint is the canonical
document store.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[test]'
cp .env.example .env
```

The application commands load `.env` automatically. Run the two components:

```bash
# Terminal 1: webhook and web interface
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Terminal 2: queue worker (calls Microsoft Graph directly)
python -m app.outlook_worker
```

Open <http://127.0.0.1:8000>. A public HTTPS URL pointing to port 8000 is
required for Microsoft Graph webhook delivery.

## Microsoft 365 sign-in and roles

The application supports Microsoft Entra ID Authorization Code login. For
production, use a dedicated single-tenant app registration:

1. In **Microsoft Entra admin center → App registrations**, create or open the
   Invoice Processor registration.
2. Under **Authentication**, add a **Web** redirect URI matching
   `AUTH_ENTRA_REDIRECT_URI`, for example:
   `https://invoice.example.com/api/auth/microsoft/callback`.
3. Under **Certificates & secrets**, create a client secret and store it in
   `AUTH_ENTRA_CLIENT_SECRET` (prefer Azure Key Vault in production).
4. In the app manifest, define these application roles with
   `allowedMemberTypes` set to `["User"]` and a unique generated GUID for each
   `id`:

   | Display name | App-role value | Program role |
   |---|---|---|
   | Invoice Processor Admin | `InvoiceProcessor.Admin` | `admin` |
   | Purchase Ledger | `InvoiceProcessor.PurchaseLedger` | `purchase_ledger` |
   | Approver 1 | `InvoiceProcessor.Approver1` | `approver1` |
   | Approver 2 | `InvoiceProcessor.Approver2` | `approver2` |
   | Purchasing | `InvoiceProcessor.Purchasing` | `purchasing` |

5. Open **Enterprise applications → Invoice Processor → Properties** and set
   **Assignment required?** to **Yes**.
6. Open **Users and groups → Add user/group**, select a user or security group,
   and assign exactly one Invoice Processor role. A user with no recognized
   role, or more than one recognized role, is denied access.
7. Configure `AUTH_ENTRA_TENANT_ID`, `AUTH_ENTRA_CLIENT_ID`,
   `AUTH_ENTRA_CLIENT_SECRET`, and `AUTH_ENTRA_REDIRECT_URI`. Set
   `AUTH_LOCAL_LOGIN_ENABLED=false` and `AUTH_COOKIE_SECURE=true`.

Assigning roles to Entra security groups requires Microsoft Entra ID licensing
that supports group-based application assignment. Direct user assignment works
without that group-assignment capability.

Local username/password login remains available for development when Entra is
not configured. It is automatically disabled when Entra is configured unless
`AUTH_LOCAL_LOGIN_ENABLED=true` is explicitly set.

## Run tests

```bash
pytest
```

## Current API

- `GET /` - invoice review interface
- `GET /api/health` - health check
- `GET /api/invoices` - received PDF invoice records
- `GET /api/invoices/{id}` - one received invoice
- `GET /api/invoices/{id}/pdf` - selected Outlook PDF
- `POST /api/outlook/notifications` - Microsoft Graph email webhook
- `POST /api/outlook/lifecycle` - Microsoft Graph subscription lifecycle webhook
- `POST /api/workflow/confirm` - calculate the confirmed invoice routing plan

## Integration boundary

SharePoint is the canonical PDF store. The worker keeps a disposable local
processing/preview cache. SQLite stores the notification queue; invoice records,
activity events, IRJ numbering, companies, suppliers, approval routes, supplier
payment settings, and process configuration can use either SQLite or PostgreSQL.

## Prepared Azure integrations

Install the optional Azure dependencies with:

```bash
python3 -m pip install -e '.[azure]'
```

Document Intelligence uses the `prebuilt-invoice` model and the existing
Invoice MCP Entra service principal. Configure
`AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`; no Document Intelligence API key is
required. Assign the service principal the **Cognitive Services User** role on
the resource.

Set `INVOICE_STORE_BACKEND=postgres` to use Azure Database for PostgreSQL for
invoice metadata, the activity feed, atomic IRJ numbering, and admin-maintained
business configuration. Configure
`AZURE_POSTGRES_HOST`, `AZURE_POSTGRES_DATABASE`, and `AZURE_POSTGRES_USER`.
When `AZURE_POSTGRES_PASSWORD` is omitted, the application obtains an Entra
token for PostgreSQL using the Invoice MCP credentials. Apply transactional
schema migrations with a DBA/deployment identity using
`python -m app.postgres_migrate`; the runtime defaults to
`AZURE_POSTGRES_AUTO_MIGRATE=false`. IT can use
`app/postgres_runtime_grants.sql.template` to grant the mapped Invoice MCP role
the required DML permissions. SQLite remains the default for offline testing.

Users and sessions remain in the existing authentication store until Entra web
sign-in is activated. Row-level security is therefore intentionally not enabled
yet.

Password-authenticated PostgreSQL deployments use a shared connection pool
instead of opening a new TLS connection for every store operation. The defaults
are one warm connection and a maximum of ten connections; override
`POSTGRES_POOL_MIN_SIZE`, `POSTGRES_POOL_MAX_SIZE`, and
`POSTGRES_POOL_TIMEOUT_SECONDS` only to match the hosting plan's connection
limits. Idle connections are retired after
`POSTGRES_POOL_MAX_IDLE_SECONDS` (five minutes by default), and connections
are checked before reuse. Entra-token database authentication continues to
create connections on demand so an expired access token is never retained by
a long-lived pool.

### Local PostgreSQL testing

SharePoint can remain the canonical PDF store while a PostgreSQL server on the
same machine stores invoice metadata, activity events, and IRJ numbering.
Install PostgreSQL 16 or newer and the optional Python dependencies, then create
a local role and database:

```bash
python3 -m pip install -e '.[azure]'
psql postgres -c "CREATE ROLE invoice_processor LOGIN PASSWORD 'local-password';"
psql postgres -c "CREATE DATABASE invoice_processing OWNER invoice_processor;"
```

Set the following values in `.env`. Transport encryption may be disabled only
for `localhost`, `127.0.0.1`, or `::1`; remote PostgreSQL connections continue
to require TLS.

```dotenv
INVOICE_STORE_BACKEND=postgres
DATABASE_URL=postgresql://invoice_processor:local-password@127.0.0.1:5432/invoice_processing?sslmode=disable
AZURE_POSTGRES_AUTO_MIGRATE=false
```

Apply the schema, import the existing SQLite configuration once, then restart
both the web application and worker:

```bash
python -m app.postgres_migrate
python -m app.postgres_config_migrate --source runtime_data/config.db
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
python -m app.outlook_worker
```

The import is transactional and idempotent, so it can be rerun safely before
retiring the SQLite configuration file. Set `CONFIG_STORE_BACKEND=sqlite` to
retain SQLite configuration while using PostgreSQL invoices; otherwise the
configuration backend follows `INVOICE_STORE_BACKEND`. Users continue to use
the authentication store during this phase.

## Direct Microsoft Graph access

The worker and subscription CLI call Microsoft Graph directly through
`app/outlook_graph.py`:

- List invoice-relevant messages and read one message's metadata.
- List and download non-inline PDF attachments.
- Convert non-inline XLS/XLSX attachments through a temporary file in the
  configured SharePoint/OneDrive drive, then delete the temporary workbook.
  Source workbooks and converted PDFs have independent configurable size limits.
- Import supplier workbooks locally from the Admin panel. Every imported row is
  scoped to the company selected by the admin; supplier account numbers preserve
  separate payment profiles for suppliers paid from multiple bank accounts.
- Create and renew the Inbox change-notification subscription.

It does not send, delete, move, or mark email as read. See
`OUTLOOK_MCP_SETUP.md` for Entra registration, mailbox restriction, environment
configuration, and the distinction between direct Graph calls and the Graph
email trigger.

## Outlook new-email webhook

The application includes Microsoft Graph webhook endpoints and subscription
management commands:

```bash
python -m app.outlook_subscription create
python -m app.outlook_subscription renew "$OUTLOOK_SUBSCRIPTION_ID"
```

Graph notifications are validated and queued without downloading the PDF inside
the webhook request. The worker performs that work through a direct Microsoft
Graph call. See `OUTLOOK_MCP_SETUP.md` for the complete live test sequence.
