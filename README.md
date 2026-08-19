# Outlook Invoice Intake

This project connects the first stage of the invoice workflow:

1. Microsoft Graph sends a webhook when an email reaches the invoice Inbox.
2. The web application queues the Outlook message ID.
3. A worker calls the read-only Outlook MCP.
4. The MCP retrieves email metadata and downloads each PDF attachment.
5. The worker creates an invoice record.
6. The web interface lists the received invoices and displays the selected PDF.

AI extraction, SharePoint upload, Sage, approvals, and payment processing are
not connected yet.

For local testing without a public HTTPS webhook, set
`OUTLOOK_LOCAL_POLLING_ENABLED=true`. The worker will poll unread messages
through the same read-only Outlook MCP. Production should continue to use Graph
webhooks.

## Implemented interface

- Received-invoice selector populated from the local invoice database.
- Real PDF preview retrieved from Outlook.
- Real source email sender, subject, and received time.
- Company, supplier, invoice number, PO number, invoice date, and route.
- Net amount, VAT, total, currency, payment terms, and due date.
- AI fields remain empty with status `Awaiting AI Extraction`.
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

The application commands load `.env` automatically. Run the three components:

```bash
# Terminal 1: read-only Outlook MCP
python -m app.outlook_mcp

# Terminal 2: webhook and web interface
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Terminal 3: queue worker
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

Downloaded PDFs are stored on the same local machine as the MCP and worker.
SQLite stores the notification queue and invoice records. Production should
replace these with managed storage/queues before processing live invoices.

## Read-only Outlook MCP

The project includes an optional custom MCP server for reading invoice email
metadata and PDF attachments through Microsoft Graph:

```bash
python -m app.outlook_mcp
```

It does not send, delete, move, or mark email as read. See
`OUTLOOK_MCP_SETUP.md` for Entra registration, mailbox restriction, environment
configuration, and the distinction between MCP tool calls and the Graph email
trigger.

## Outlook new-email webhook

The application includes Microsoft Graph webhook endpoints and subscription
management commands:

```bash
python -m app.outlook_subscription create
python -m app.outlook_subscription renew "$OUTLOOK_SUBSCRIPTION_ID"
```

Graph notifications are validated and queued without downloading the PDF inside
the webhook request. The worker performs that work through the MCP. See
`OUTLOOK_MCP_SETUP.md` for the complete live test sequence.
