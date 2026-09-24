# General Invoice Process

## Purpose

This is the operational reference for the process implemented by the Invoice
Processor. The detailed state diagram is in `INVOICE_WORKFLOW_FLOWCHART.md`.

## System responsibilities

- SharePoint stores the canonical PDF.
- Azure Database for PostgreSQL stores invoice metadata, configuration,
  workflow state, extraction feedback, queue state, sessions, and audit events.
- Microsoft Graph reads the shared mailbox, receives webhook events, stores
  files in SharePoint, and sends configured notifications.
- Microsoft Entra authenticates users and supplies application roles.
- The extraction service proposes fields and confidence. Purchase Ledger
  remains responsible for corrections and confirmation.

## 1. Intake

Invoices enter through Outlook, manual upload, or SharePoint Incoming. One email
may contain multiple PDFs; each supported attachment creates its own record.
Graph webhooks are the production trigger. Local polling is available for
development and recovery. With lookback `0`, it starts at midnight today in
Europe/London. PostgreSQL idempotency prevents duplicate records.

The worker uploads the document to SharePoint Incoming and stores its source
identifiers, sender, subject, received time, original filename, size,
SharePoint item ID, and URL in PostgreSQL. Worker heartbeat, attempts, and
failures are visible in Admin.

## 2. Extraction and review

The model proposes document type, company, supplier, supplier invoice number,
invoice date, total, currency, PO number, and confidence. Unreadable documents,
missing fields, low confidence, ambiguity, and unknown companies or suppliers
enter `Needs Review`.

The review screen displays the PDF beside editable fields, confidence, warnings,
and the proposed route. PostgreSQL keeps original and corrected values, reason,
reviewer, model version, prompt version, and final route. This is evaluation
data; the application does not retrain a model automatically.

Every document in `Needs Review` is moved to the shared SharePoint folder
`Invoices/Flagged Invoices`. Resolving the review moves it to the appropriate
workflow folder; rejecting or filing a statement moves it to its final folder.

## 3. Confirmation, duplicates, and IRJ

Confirmation validates required values and supplier invoice-number rules, then
checks for duplicates. A suspected duplicate requires an explicit decision to
cancel it or continue it as a separate invoice.

Companies use automatic or manual six-digit IRJs. Automatic numbers are
allocated transactionally in PostgreSQL. Manual numbers are validated for
format and uniqueness. The final filename is `IRJ-original-filename.pdf`;
temporary Outlook hash prefixes are removed.

## 4. Routing

An invoice with a PO enters `Awaiting PO Matching`. A query records its
category, notes, and contact without advancing the invoice. An explicit match
moves it to Sage registration.

An invoice without a PO enters `Awaiting Sage Registration`. After Sage
registration, the company and supplier approval matrix determines one or two
sequential approvers. A missing route or required email sends the invoice to
review. Editing an existing company and supplier pair updates that route.

## 5. Approval, payment, and reconciliation

Approval requests are sent once per stage. A query or hold stays at the same
stage and preserves comments. Resolving it does not resend a successful stage
email. Rejections record the actor, time, comments, and reason.

After final approval, Purchase Ledger chooses BACS/Bankline or Foreign POA.
Payment records require the applicable date, reference, method, and actor.
Foreign allocation is recorded separately where required. A paid invoice
remains open until bank reconciliation is deliberately confirmed.

## 6. Audit and operations

Every meaningful transition writes an audit event. IRJ search shows the current
metadata, status, notes, SharePoint link, and chronological history.

Admin manages companies, suppliers, approval routes, terms, process settings,
worker status, and failed queue retries. Power BI reads the three PostgreSQL
reporting views through a dedicated read-only identity.

## Exception rules

- Never guess an ambiguous company, supplier, approver, invoice number, or
  payment decision.
- Preserve the PDF and metadata when processing fails.
- Keep queries and holds at their current workflow stage.
- Make retries idempotent.
- Require an authorised human action for duplicate overrides, Sage
  registration, approval, payment, and reconciliation.

## Remaining integrations

- Select and configure the production extraction model. Azure Document
  Intelligence custom extraction is the identified next training option.
- Connect Sage 200 posting and remittance APIs.
- Deploy and validate the Power BI semantic model and service refresh.
- Add production alert delivery and agreed retention policies.
