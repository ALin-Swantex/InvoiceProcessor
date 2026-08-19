# Outlook MCP Setup

## Important architecture note

The official Microsoft MCP Server for Enterprise is currently a preview focused
on read-only Microsoft Entra directory data. It does not currently provide an
Outlook invoice-email trigger.

This project therefore includes a private read-only MCP server that calls
Microsoft Graph. It exposes four tools:

- `list_invoice_emails`
- `get_invoice_email`
- `list_pdf_attachments`
- `download_pdf_attachment`

The tools cannot send, delete, move, or mark email as read.

MCP is request/response based; it does not push an event when a new message
arrives. The project now uses a Microsoft Graph change-notification webhook for
that trigger.

## Automatic new-email trigger

The web application exposes:

- `POST /api/outlook/notifications`
- `POST /api/outlook/lifecycle`

When Graph reports a newly created Inbox message, the notification endpoint:

1. Answers Microsoft's `validationToken` challenge in plain text.
2. Verifies the subscription `clientState`.
3. Extracts the Outlook message ID.
4. Adds it to an idempotent SQLite queue.
5. Returns `202 Accepted` immediately.

It deliberately does not download or process the invoice inside the webhook
request. Microsoft expects a response within three seconds. The included worker
claims the queued event, calls the Outlook MCP, downloads its PDFs, and creates
the invoice records used by the web interface.

## 1. Register an Entra application

Ask a Microsoft 365 administrator to create an app registration for the invoice
integration and record:

- `[MICROSOFT ENTRA TENANT ID]`
- `[APPLICATION CLIENT ID]`
- `[APPLICATION CLIENT SECRET]`
- `[INVOICE MAILBOX ADDRESS]`

For this read-only stage, grant Microsoft Graph **Application** permission
`Mail.Read` and provide admin consent.

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

For production, use a certificate or managed identity instead of a long-lived
client secret.

## 3. Install and run the MCP server

```bash
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m app.outlook_mcp
```

The default Streamable HTTP endpoint is:

```text
http://127.0.0.1:8001/mcp
```

The server validates its Outlook configuration on the first tool call. It will
fail clearly if any required value is missing.

Keep this terminal running.

## 4. Run the web application and worker

In a second terminal, run:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

In a third terminal, run:

```bash
python -m app.outlook_worker
```

The worker polls the local notification queue, connects to
`OUTLOOK_MCP_URL`, and stores received invoice records in `INVOICE_DB_PATH`.

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
read-only MCP instead:

```dotenv
OUTLOOK_LOCAL_POLLING_ENABLED=true
```

Restart the worker after changing this value. This lets a test email reach the
same queue, PDF download, invoice database, and frontend without exposing the
application publicly. Keep this disabled in production, where Graph webhooks
provide the deterministic trigger.

## 6. Renew the Graph subscription

Outlook message subscriptions expire in under seven days. Renew the subscription
at least daily in production:

```bash
python -m app.outlook_subscription renew "$OUTLOOK_SUBSCRIPTION_ID"
```

Use an Azure scheduled Function or equivalent managed job and alert if renewal
fails. If a subscription expires, create a new one and reconcile the mailbox for
messages received during the gap.

## 7. Local queue and invoice storage

Accepted events are stored by default in:

```text
runtime_data/outlook_notifications.db
```

Duplicate deliveries are ignored using the subscription, resource and change
type as an idempotency key.

Worker failures are retained with `failed` status, attempt count, and the most
recent error rather than being discarded. Successful PDF records are stored by
default in:

```text
runtime_data/invoices.db
```

The invoice table is idempotent on Outlook message ID plus attachment ID, so a
duplicate Graph delivery does not create a duplicate invoice.

SQLite is for local development only. Production should use Azure SQL with Azure
Service Bus or Storage Queue before processing live invoices.

## 8. Configure an MCP client

MCP client configuration formats differ. Use these generic values:

```json
{
  "name": "invoice-outlook",
  "transport": "streamable-http",
  "url": "http://127.0.0.1:8001/mcp"
}
```

Keep the endpoint bound to `127.0.0.1` during development. Do not expose it to a
network without authentication, TLS, request limits, and an explicit deployment
review.

## 9. Test the complete email-to-screen flow

Run the mocked test suite before using credentials:

```bash
pytest
```

After tenant approval:

1. Confirm the MCP, web application, worker, and HTTPS tunnel are running.
2. Set `OUTLOOK_WEBHOOK_URL` and `OUTLOOK_LIFECYCLE_URL` to the public HTTPS
   endpoints.
3. Create the Graph subscription.
4. Send a new email with a valid PDF attachment to the configured invoice
   mailbox Inbox.
5. Wait a few seconds for webhook delivery and worker processing.
6. Open <http://127.0.0.1:8000>.
7. Select the received invoice and confirm its sender, subject, received time,
   filename, and PDF are displayed.

If the invoice does not appear, inspect the local queues without exposing email
contents:

```bash
sqlite3 runtime_data/outlook_notifications.db \
  "select id,message_id,status,attempts,last_error from outlook_notifications order by id desc limit 10;"

sqlite3 runtime_data/invoices.db \
  "select id,message_id,original_filename,status from invoices order by id desc limit 10;"
```

An email without a PDF, an MCP failure, or a Graph download error is retained as
`failed` with `last_error`. It is not silently discarded.

## 10. Next production step

After read-only access is proven:

1. Replace local SQLite and filesystem storage with managed production services.
2. Save each PDF to SharePoint Incoming Invoices.
3. Send each PDF to the approved extraction API.
4. Store and display the structured extraction result.
5. Add retry policy, monitoring, alerts, and an authenticated exception screen.

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
