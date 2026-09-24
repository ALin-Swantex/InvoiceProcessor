# Outlook Graph Setup

## Important architecture note

The official Microsoft MCP Server for Enterprise is currently a preview focused
on read-only Microsoft Entra directory data. It does not currently provide an
Outlook invoice-email trigger.

This project therefore calls Microsoft Graph directly from `app/outlook_graph.py`.
The `OutlookGraphClient` reads invoice mail and supports these attachment
operations:

- `list_invoice_emails`
- `get_invoice_email`
- `list_invoice_attachments`
- `download_invoice_attachment`
- `list_pdf_attachments`
- `download_pdf_attachment`

Excel conversion temporarily creates and deletes a workbook in the configured
SharePoint/OneDrive drive. It does not alter or delete the original email
attachment, send email, or mark email as read.

Microsoft Graph webhooks push the new-email event; the worker then calls Graph
directly (no separate MCP server, request/response tool layer, or additional
hop is required).

## Automatic new-email trigger

The web application exposes:

- `POST /api/outlook/notifications`
- `POST /api/outlook/lifecycle`

When Graph reports a newly created Inbox message, the notification endpoint:

1. Answers Microsoft's `validationToken` challenge in plain text.
2. Verifies the subscription `clientState`.
3. Extracts the Outlook message ID.
4. Adds it to the idempotent PostgreSQL queue.
5. Returns `202 Accepted` immediately.

It deliberately does not download or process the invoice inside the webhook
request. Microsoft expects a response within three seconds. The included worker
claims the queued event, calls Microsoft Graph directly, downloads PDFs or
converts supported Excel workbooks, and creates the invoice records used by the
web interface.

## 1. Register an Entra application

Ask a Microsoft 365 administrator to create an app registration for the invoice
integration and record:

- `[MICROSOFT ENTRA TENANT ID]`
- `[APPLICATION CLIENT ID]`
- `[APPLICATION CLIENT SECRET]`
- `[INVOICE MAILBOX ADDRESS]`

Grant Microsoft Graph **Application** permission `Mail.Read` and provide admin
consent. Excel conversion also requires write access to the selected temporary
conversion drive. Prefer `Sites.Selected` with an explicit write grant on only
the invoice-processing SharePoint site. If the organisation does not use
resource-specific site grants, the broader alternative is
`Files.ReadWrite.All`; this should only be approved after IT security review.

Application `Mail.Read` can otherwise access every mailbox in the tenant. The
administrator must restrict the service principal to the invoice shared mailbox
using Exchange Online Application RBAC or the organisation's approved mailbox
access-policy mechanism.

Do not grant `Mail.ReadWrite` or `Mail.Send` for this stage.

## 2. Configure local environment values

```bash
cp .env.example .env
```

Replace the placeholders in `.env`. Never commit this file.

The project entry points load this `.env` file automatically.

For Excel invoices, set `EXCEL_CONVERSION_DRIVE_ID` to the document-library
drive ID and create the folder configured by
`EXCEL_CONVERSION_TEMP_FOLDER` (default: `Invoice Conversion`). When the
conversion drive is the same as `SHAREPOINT_DRIVE_ID`,
`EXCEL_CONVERSION_DRIVE_ID` may be omitted because the worker falls back to
that value. `OUTLOOK_MCP_MAX_EXCEL_BYTES` limits the source workbook upload
(default: 50 MiB), while `OUTLOOK_MCP_MAX_PDF_BYTES` independently limits the
converted PDF.

For production, use a certificate or managed identity instead of a long-lived
client secret.

## 3. Install dependencies

```bash
source .venv/bin/activate
python -m pip install -e '.[test]'
```

The worker and subscription CLI validate the Outlook configuration on first
use. They fail clearly if any required value is missing.

## 4. Run the web application and worker

In one terminal, run:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

In a second terminal, run:

```bash
python -m app.outlook_worker
```

The worker polls the PostgreSQL notification queue, calls Microsoft Graph directly
using the `OUTLOOK_MCP_TENANT_ID` / `OUTLOOK_MCP_CLIENT_ID` /
`OUTLOOK_MCP_CLIENT_SECRET` / `OUTLOOK_MCP_MAILBOX` settings, and stores
received invoice records and runtime state in Azure PostgreSQL.

## 5. Create the Graph subscription

The web application must be running at the configured public HTTPS URL.
`localhost` cannot receive Microsoft Graph notifications.

For development, use an organisation-approved HTTPS development tunnel. For
production, deploy the application to Azure App Service, Container Apps or
Functions.

Create the Inbox subscription:

```bash
python -m app.outlook_subscription create
```

Graph calls `OUTLOOK_WEBHOOK_URL` with a validation token during creation. If
validation succeeds, the command prints the subscription ID and expiration.
Store the ID locally as `OUTLOOK_SUBSCRIPTION_ID`.

The subscription listens only for newly created messages in the configured
mailbox Inbox.

### Local test without a public webhook

For local development only, the worker can poll unread messages through the
same direct Microsoft Graph connection instead:

```dotenv
OUTLOOK_LOCAL_POLLING_ENABLED=true
OUTLOOK_LOCAL_POLLING_LOOKBACK_DAYS=0
```

Restart the worker after changing this value. This lets a test email reach the
same queue, PDF download, invoice database, and frontend without exposing the
application publicly. Keep this disabled in production, where Graph webhooks
provide the deterministic trigger.

Local polling reads only unread messages with attachments from the Inbox,
newest first, beginning at midnight today in Europe/London. Set the lookback to
`1` or more only when recovering messages after an outage. Successfully queued
message identities are retained in PostgreSQL, so restarting the worker does
not create a second queue item. Messages remain in the Inbox because this
deployment uses `Mail.Read`; moving them would require the broader
`Mail.ReadWrite` application permission.

## 6. Renew the Graph subscription

Outlook message subscriptions expire in under seven days. Renew the subscription
at least daily in production:

```bash
python -m app.outlook_subscription renew "$OUTLOOK_SUBSCRIPTION_ID"
```

Use an Azure scheduled Function or equivalent managed job and alert if renewal
fails. If a subscription expires, create a new one and reconcile the mailbox for
messages received during the gap.

## 7. Queue and invoice storage

Accepted events, attempts, errors, worker heartbeat, invoice metadata, and
workflow state are stored in Azure PostgreSQL. Duplicate deliveries are ignored
using the subscription, resource, and change type. Invoice creation is also
idempotent on Outlook message ID plus attachment ID. PDFs are stored in
SharePoint; the worker's local download and SharePoint cache directories are
temporary processing storage.

Admin shows worker health and queue counts and permits an authorised retry of a
failed queue item. A retry reuses the idempotency keys.

## 8. Test the complete email-to-screen flow

Run the mocked test suite before using credentials:

```bash
pytest
```

After tenant approval:

1. Confirm the web application, worker, and HTTPS tunnel are running.
2. Set `OUTLOOK_WEBHOOK_URL` and `OUTLOOK_LIFECYCLE_URL` to the public HTTPS
   endpoints.
3. Create the Graph subscription.
4. Send a new email with a valid PDF attachment to the configured invoice
   mailbox Inbox.
5. Wait a few seconds for webhook delivery and worker processing.
6. Open <http://127.0.0.1:8000>.
7. Select the received invoice and confirm its sender, subject, received time,
   filename, and PDF are displayed.

If the invoice does not appear, inspect the Admin worker panel and application
logs without exposing email contents. A database administrator can also inspect:

```sql
SELECT id, message_id, status, attempts, last_error
FROM outlook_notifications ORDER BY id DESC LIMIT 10;

SELECT id, message_id, original_filename, status
FROM invoices ORDER BY id DESC LIMIT 10;
```

An email without a PDF or a Graph download error is retained as `failed` with
`last_error`. It is not silently discarded.

## 9. Remaining production work

PostgreSQL persistence, SharePoint storage, extraction review, retry handling,
and worker visibility are implemented. Remaining deployment work is to select
and validate the production extraction model, deploy the web app and worker
under process supervision, automate subscription renewal, and connect
production alert delivery.

Do not use an LLM or MCP tool call as the event trigger itself. Keep triggering,
idempotency, retries, and audit handling deterministic.

## References

- Microsoft MCP Server for Enterprise overview:
  <https://learn.microsoft.com/en-us/graph/mcp-server/overview>
- Microsoft MCP Server for Enterprise setup:
  <https://learn.microsoft.com/en-us/graph/mcp-server/get-started>
- Microsoft Graph attachment API:
  <https://learn.microsoft.com/en-us/graph/api/attachment-get>
- Microsoft Graph permissions reference:
  <https://learn.microsoft.com/en-us/graph/permissions-reference>
- Microsoft Graph webhook delivery:
  <https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks>
- Microsoft Graph subscription lifetime:
  <https://learn.microsoft.com/en-us/graph/api/resources/subscription>
