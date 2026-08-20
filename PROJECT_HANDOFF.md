# Invoice Processing Project Handoff

Last updated: 19 August 2026

## Project goal

Build a custom web application for processing supplier invoices while retaining
Microsoft 365 for email, document storage, identity, and notifications.

The intended production responsibilities are:

- Outlook receives invoice emails.
- Microsoft Graph detects new messages.
- The application retrieves PDF attachments.
- SharePoint stores the PDFs and workflow metadata.
- An AI service extracts invoice fields.
- Purchase Ledger reviews and confirms the invoice.
- Backend workflow logic routes PO and non-PO invoices.
- Microsoft 365 accounts provide user authentication.

## Agreed invoice workflow

1. Invoice received by email.
2. PDF saved to SharePoint Incoming Invoices.
3. Invoice information extracted.
4. Company being invoiced identified.
5. Purchase Ledger reviews and confirms the invoice.
6. IRJ reference generated.
7. System determines whether a PO number exists.

### PO invoice

1. Move to Purchase Order Invoice Matching.
2. Notify Purchase Ledger.
3. Purchase Ledger compares the invoice with the PO and goods received data.
4. Correct matches move to Approved.
5. Differences remain outstanding with a query assigned to Purchasing.
6. Resolved invoices move to Approved.

### Non-PO invoice

1. Move to the relevant company area with the IRJ-prefixed filename.
2. Identify supplier and approvers from the company approval matrix.
3. Notify Approver 1.
4. Send to Approver 2 when required.
5. Notify Purchase Ledger after full approval.
6. Move to Approved.

### After approval

1. Purchase Ledger processes payment and records the payment date.
2. Invoice moves to Bank Reconciliation.
3. Purchase Ledger confirms reconciliation.
4. Invoice moves to Complete / Filed.

## Current working implementation

The first Outlook intake stage is working locally:

1. The worker polls unread Outlook messages through the read-only MCP.
2. The MCP retrieves email metadata and PDF attachment details from Graph.
3. The PDF is downloaded.
4. An invoice record is created.
5. The web interface displays the PDF and source email metadata.

The live test email and `invoice.pdf` were retrieved successfully.

The frontend automatically refreshes its invoice list every five seconds.

Current web address:

```text
http://127.0.0.1:8000
```

## What is not connected yet

- AI invoice-field extraction
- SharePoint upload and file movement
- Company identification
- IRJ generation
- PO-number detection
- Purchase Ledger notifications
- Approvals and approval matrices
- Sage integration
- Payment and bank reconciliation
- Microsoft 365 user login
- Production monitoring and hosting

The displayed invoice currently has status:

```text
Awaiting AI Extraction
```

## Outlook architecture

### Production trigger

Microsoft Graph change-notification webhooks are the intended production
trigger. MCP is a request/response tool server and is not itself an email event
trigger.

Graph webhook endpoints already exist:

```text
POST /api/outlook/notifications
POST /api/outlook/lifecycle
```

A live Graph subscription requires:

- A publicly reachable HTTPS application URL
- `OUTLOOK_WEBHOOK_URL`
- `OUTLOOK_LIFECYCLE_URL`
- `OUTLOOK_WEBHOOK_CLIENT_STATE`
- A created and regularly renewed Graph subscription

This has not been configured because the application currently runs only on
localhost.

### Local testing trigger

Local MCP polling is enabled:

```dotenv
OUTLOOK_LOCAL_POLLING_ENABLED=true
```

This allows local testing without exposing the web application publicly. It
polls unread messages and feeds them into the same processing pipeline.

Production should disable local polling and use Graph webhooks.

## Outlook permissions and security

- Current Graph application permission: `Mail.Read`
- Do not add `Mail.ReadWrite` or `Mail.Send` at this stage.
- Restrict the application to the invoice mailbox through Exchange application
  RBAC or the organisation's approved mailbox access policy.
- The client secret is stored only in local `.env`.
- `.env` is ignored by Git.
- Never paste credentials into chat or commit them.
- Prefer a certificate or managed identity in production.

The previous invalid secret was replaced with a valid Entra client secret
**Value**. Secret IDs cannot be used for authentication.

## Temporary local storage

The prototype currently uses:

```text
runtime_data/outlook_notifications.db
runtime_data/invoices.db
outlook_downloads/
```

Purpose:

- `outlook_notifications.db`: queue status, attempts, errors, and duplicate
  prevention.
- `invoices.db`: email metadata, PDF location, and frontend processing status.
- `outlook_downloads/`: locally downloaded PDF files.

These are development-only implementation details.

## Intended production storage

A conventional relational database is not mandatory.

The preferred Microsoft 365 design is:

- SharePoint document library for all PDFs
- SharePoint document metadata or an Invoice Register List for invoice status
- SharePoint Lists for companies, suppliers, approvers, limits, rules, and
  notification settings
- SharePoint List or another controlled store for workflow/audit history
- Azure Storage Queue or Service Bus for reliable webhook processing and retry
  handling

Folders alone should not be the only workflow record. Duplicate detection,
errors, approval state, retries, and audit history need structured metadata, but
that metadata can be stored in SharePoint rather than a SQL database.

## AI recommendation

Use Azure AI Document Intelligence's prebuilt invoice model as the primary
extractor because it is designed for invoices and fits the Microsoft/Azure
environment.

Potential extracted fields:

- Company being invoiced
- Supplier
- Supplier invoice number
- PO number
- Invoice date
- Net amount
- VAT amount
- Total amount
- Currency
- Payment terms
- Due date
- Confidence values and review warnings

Claude may later be considered as a fallback for low-confidence or unusual
documents after privacy and data-processing review. It is not currently
connected.

## Local startup instructions

From:

```text
/Users/andy/Desktop/Internship
```

Install dependencies when required:

```bash
.venv/bin/python -m pip install -e '.[test]'
```

Run the two components in separate terminals:

```bash
# Terminal 1: web application
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# Terminal 2: worker (calls Microsoft Graph directly; local polling optional)
.venv/bin/python -m app.outlook_worker
```

The application loads `.env` automatically. Activating the virtual environment
is optional when using the explicit `.venv/bin/...` commands.

Health check:

```bash
curl http://127.0.0.1:8000/api/health
```

Expected result:

```json
{"status":"ok","mode":"outlook-intake"}
```

Received invoice API:

```bash
curl http://127.0.0.1:8000/api/invoices
```

## Tests

Run:

```bash
.venv/bin/python -m pytest -q
```

Current result:

```text
26 passed
```

There is one existing Starlette TestClient deprecation warning relating to
HTTPX. It does not currently break the tests.

## Important implementation files

- `app/main.py`
  - Web interface, invoice APIs, PDF endpoint, webhook endpoints, and workflow
    confirmation API.
- `app/outlook_graph.py`
  - Microsoft Graph authentication and Outlook operations (email metadata, PDF
    attachment retrieval, subscription create/renew), called directly by the
    worker and subscription CLI. No MCP server is involved.
- `app/outlook_worker.py`
  - Queue processing and optional local mailbox polling, using
    `OutlookGraphRetriever` to call Microsoft Graph directly.
- `app/outlook_notifications.py`
  - Temporary SQLite notification queue.
- `app/invoices.py`
  - Temporary SQLite invoice records.
- `app/outlook_subscription.py`
  - Graph subscription creation and renewal.
- `app/workflow.py`
  - Hidden PO/non-PO routing decisions.
- `.env.example`
  - Configuration template without credentials.
- `OUTLOOK_MCP_SETUP.md`
  - Detailed Outlook, webhook, subscription, and testing instructions.
- `CUSTOM_CODE_PROPOSAL.md`
  - Business proposal covering design, hosting, AI, costs, maintenance,
    ownership, security, reliability, backup, and Power Automate comparison.
- `SOFTWARE_SPEC.md`
  - Original detailed software specification.
- `GENERAL_PROCESS.md`
  - Expanded invoice workflow.

## Recommended next development sequence

1. Connect SharePoint and upload each retrieved PDF directly to Incoming
   Invoices.
2. Replace the local PDF path in the frontend with a protected SharePoint/Graph
   document endpoint.
3. Add Azure AI Document Intelligence extraction.
4. Store extracted fields and confidence metadata in SharePoint.
5. Add Purchase Ledger review and confirmation.
6. Add company configuration and IRJ numbering.
7. Execute the existing PO/non-PO routing plan against SharePoint.
8. Add approval matrices and notification rules through an admin interface or
   controlled SharePoint Lists.
9. Deploy to Azure and replace local polling with Graph webhooks.
10. Add Microsoft Entra ID login, production queues, monitoring, backups, and
    exception management.

## Tomorrow's suggested starting point

Start with the SharePoint integration because it removes the temporary local PDF
and invoice storage and establishes the intended source of truth before adding
AI extraction.

Required SharePoint decisions:

- Target site URL
- Incoming Invoices document library/folder
- Company folder structure
- Invoice Register List or document metadata columns
- Application permissions and mailbox/site access restrictions
- Whether the organisation prefers SharePoint Lists or a managed database for
  workflow and audit metadata
