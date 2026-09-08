# Outlook Invoice Intake

This project connects the first stage of the invoice workflow:

1. Microsoft Graph sends a webhook when an email reaches the invoice Inbox.
2. The web application queues the Outlook message ID.
3. A worker calls Microsoft Graph directly to read the email and download each
   PDF attachment, or convert an XLS/XLSX attachment to PDF.
4. The worker creates an invoice record and, when configured, sends the PDF
   to Azure AI Document Intelligence using the Invoice MCP Entra identity.
5. The web interface lists the received invoices and displays the selected PDF.

SharePoint permissions, live Azure resources, Sage, and outbound email delivery
are not connected yet.

For local testing without a public HTTPS webhook, set
`OUTLOOK_LOCAL_POLLING_ENABLED=true`. The worker will poll unread messages
through the same direct Microsoft Graph connection. Production should continue
to use Graph webhooks.

## Implemented interface

- Received-invoice selector populated from the local invoice database.
- Real PDF preview retrieved from Outlook.
- Real source email sender, subject, and received time.
- Company, supplier, invoice number, PO number, invoice date, and route.
- Net amount, VAT, total, currency, payment terms, and due date.
- Azure's `prebuilt-invoice` fields populate the review form when
  `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` is configured.
- Company, supplier, supplier invoice number, invoice date, total, and currency
  are critical extraction fields; missing or low-confidence values are sent to
  Purchase Ledger review.
- Disabled Purchase Ledger confirmation until real data and workflow endpoints are connected.
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
- The routing function does not move files or send email. Future Outlook,
  SharePoint, or MCP adapters will execute the returned plan.

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

Downloaded PDFs are stored on the same local machine as the worker. SQLite
stores the notification queue and invoice records. Production should replace
these with managed storage/queues before processing live invoices.

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
invoice metadata, the activity feed, and atomic IRJ numbering. Configure
`AZURE_POSTGRES_HOST`, `AZURE_POSTGRES_DATABASE`, and `AZURE_POSTGRES_USER`.
When `AZURE_POSTGRES_PASSWORD` is omitted, the application obtains an Entra
token for PostgreSQL using the Invoice MCP credentials. Apply transactional
schema migrations with a DBA/deployment identity using
`python -m app.postgres_migrate`; the runtime defaults to
`AZURE_POSTGRES_AUTO_MIGRATE=false`. IT can use
`app/postgres_runtime_grants.sql.template` to grant the mapped Invoice MCP role
the required DML permissions. SQLite remains the default for offline testing.

The first PostgreSQL phase moves invoices, activity events, and IRJ numbering.
Company, supplier, approval, user, and company-access tables are included in
the authoritative schema but remain on the existing local stores until their
PostgreSQL adapters and Entra web sign-in are activated. Row-level security is
therefore intentionally not enabled yet.

## Direct Microsoft Graph access

The worker and subscription CLI call Microsoft Graph directly through
`app/outlook_graph.py`:

- List invoice-relevant messages and read one message's metadata.
- List and download non-inline PDF attachments.
- Convert non-inline XLS/XLSX attachments through a temporary file in the
  configured SharePoint/OneDrive drive, then delete the temporary workbook.
  Source workbooks and converted PDFs have independent configurable size limits.
- Import supplier workbooks locally from the Admin panel. Each trading partner
  is stored as a supplier company and applies across all invoice companies by
  default; supplier account numbers preserve separate payment profiles for
  suppliers paid from multiple bank accounts.
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
