# Invoice Processor Software Specification

## Status and goals

This maintained specification describes the current FastAPI application,
cross-platform launcher, and Outlook/SharePoint worker. The system must capture
every supported invoice, keep one authoritative metadata record and PDF,
review uncertain extraction, route invoices through auditable decisions,
prevent duplicate processing and notifications, and expose operational and
reporting data.

## Users

| Role | Responsibilities |
| --- | --- |
| Admin | Configuration, approval routes, worker monitoring, failed-item retry |
| Purchase Ledger | Review, PO matching, Sage confirmation, payment, reconciliation |
| Approver 1/2 | Sequential approval decisions and comments |
| Purchasing | Investigate PO queries |

Microsoft Entra authenticates production users and supplies one supported app
role per account. Local password login is for controlled tests and development.

## Architecture

| Component | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI UI, API, role enforcement, admin and review screens |
| `app/outlook_worker.py` | Queue, Outlook polling, SharePoint intake, heartbeat |
| `app/invoice_lifecycle.py` | Validation, transitions, filing, notifications, audit |
| `app/postgres_*.py` | Production persistence, migrations, monitoring, queues |
| `app/outlook_graph.py` | Mail and attachment access through Graph |
| `app/sharepoint.py` | Canonical PDF storage and movement |
| `app/ai_extraction.py` | Extraction adapter and confidence data |
| `app/launcher.py` | Desktop service start and stop |

SharePoint owns document bytes. Azure PostgreSQL owns application metadata and
runtime state. Local SQLite is not a production source of truth. SQLite classes
remain only as isolated test doubles.

## Functional requirements

### Intake and extraction

- Accept all supported PDF attachments, including multiple PDFs per email.
- Validate type and size and store source identifiers for idempotency.
- Upload to SharePoint Incoming before workflow processing.
- Poll from today by default during local recovery.
- Store queue errors, attempts, and worker heartbeat in PostgreSQL.
- Store extracted fields, confidence, warnings, model, and prompt version.
- For invoices longer than three pages, send only the first and final pages to
  Azure extraction; the first page supplies identity fields and the final page
  supplies the invoice total.
- Route missing, ambiguous, invalid, unknown, or low-confidence results to
  review.
- Show PDF evidence beside editable fields and a clear routing explanation.
- Preserve original and corrected values, reason, reviewer, and timestamps.

### Workflow

- Detect likely duplicates before normal routing.
- Use unique six-digit automatic or validated manual IRJs.
- Route PO invoices through matching and Sage registration.
- Route nominal invoices through Sage and the approval matrix.
- Update an existing approval route when its emails are edited.
- Support one or two approvers, holds, resumptions, and rejection.
- Restrict each approver's invoice list, detail, PDF, search, activity, and
  decisions to rows assigned to their verified Entra email.
- Send each successful stage notification at most once per invoice.
- Remind the assigned approver every seven calendar days while pending and
  every 30 calendar days while their approval is on hold.
- Notify Purchase Ledger when an invoice is rejected.
- Route approved invoices automatically from the supplier payment setting;
  missing, unsupported, or conflicting settings place the invoice on hold.
- Allow permanent deletion only for a possible duplicate in Incoming review.
- Separate payment, foreign allocation, and reconciliation.
- Record each meaningful action in the audit trail.

### Search, administration, and reporting

- Search by IRJ and show current stage plus chronological history.
- Manage companies, suppliers, aliases, routes, terms, and settings.
- Display worker health and queues and allow an authorised failed-item retry.
- Publish read-only invoice, event, and correction views for Power BI.

## Security and reliability

- Verify TLS for Azure PostgreSQL and use HTTPS for Microsoft services.
- Keep secrets outside version control.
- Restrict Graph mail access to the shared mailbox and prefer
  `Sites.Selected` for SharePoint.
- Separate database roles for migrations, runtime, and Power BI.
- Webhooks acknowledge quickly and defer work to an idempotent queue.
- Restarts preserve workflow, audit, queue, IRJ, and session state.
- One failed document does not stop later work.
- API failures show the endpoint and status instead of parsing HTML as JSON.
- Health monitoring detects a stale worker.

## Implemented

- Outlook webhook, today-only polling, multiple attachments, and SharePoint
- PostgreSQL persistence and migrations
- Extraction review, correction feedback, and routing explanation
- Duplicate review, IRJs, and clean final filenames
- PO, approval, payment, reconciliation, and audit workflows
- Recurring approval reminders, rejection notifications, and email queue links
- Automatic supplier payment routing with visible exception holds
- Entra roles, worker visibility, retries, and approval-matrix editing
- Three Power BI reporting views and a cross-platform launcher
- Automated tests with mocked external services

## Remaining work

| Item | Completion evidence |
| --- | --- |
| Production extraction | Labelled invoice set meets agreed accuracy and review targets |
| Sage 200 | Sandbox posting, idempotency, and recovery verified |
| Power BI deployment | Reader, published model, refresh, counts, and access verified |
| Production hosting | HTTPS, supervision, secrets, alerts, backup/restore complete |
| UAT | Required cases in `USER_ACCEPTANCE_TEST_CASES.md` signed off |

Passing local tests does not prove live Azure, Graph, SharePoint, extraction, or
Power BI service connectivity. Live smoke checks must use approved credentials
and non-destructive test resources.

## Decisions

- SharePoint is the PDF source of truth.
- PostgreSQL is the production metadata and runtime source of truth.
- Humans confirm uncertain extraction and consequential finance actions.
- Admin data replaces hard-coded company and supplier routes.
- Corrections support evaluation; retraining is a governed offline process.
- Power BI uses read-only views.
