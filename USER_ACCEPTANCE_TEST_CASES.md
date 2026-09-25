# User Acceptance Test Cases

Use this checklist against a non-production mailbox, SharePoint site, and
PostgreSQL database. Do not use live supplier, payment, or customer data for
testing. Record the invoice ID/IRJ, tester, date, result, and evidence for each
case.

## Test data to prepare

| Label | Test data |
| --- | --- |
| Company A | A configured company with complete SharePoint folders |
| Supplier A | Configured supplier and alias, with one approver |
| Supplier B | Configured supplier, with two approvers |
| Supplier C | Configured supplier with no approval-matrix route |
| Invoice 1 | Clear, one-page PDF for Supplier A; no PO number |
| Invoice 2 | Clear PDF for Supplier B; valid PO number |
| Invoice 3 | Clear PDF whose company, supplier, or invoice number is ambiguous |
| Invoice 4 | A second copy of Invoice 1 |
| Statement 1 | Supplier statement PDF, not an invoice |
| Invalid file | A renamed non-PDF file and a corrupt PDF |

For each invoice, use a distinct supplier invoice number unless the test is for
duplicates. Ensure approver test accounts can receive mail and log in.

## Core journeys

| ID | Scenario | Steps | Expected result |
| --- | --- | --- | --- |
| UAT-01 | Manual nominal invoice | Upload Invoice 1; review extraction; confirm fields; confirm Sage registration; approve as Approver 1; route BACS; record payment; reconcile. | One record progresses through `Awaiting AI Extraction` → `Awaiting Sage Registration` → `Awaiting Approval 1` → `Approved` → payment → `Reconciled / Complete`. The PDF is moved at each filing step and its audit history is complete. |
| UAT-02 | Manual PO invoice | Upload Invoice 2; confirm fields; record a successful PO match; confirm Sage registration. | The invoice enters `Awaiting PO Matching`, then `Awaiting Sage Registration`, then `Approved`. It moves to the PO folder and then the Approved folder. |
| UAT-03 | Two-stage approval | Process a nominal invoice for Supplier B through Sage registration; approve as Approver 1, then Approver 2. | It moves from Approval 1 to Approval 2 only after the first decision. Both decisions, users, times, comments, and notification events are recorded. |
| UAT-04 | Outlook intake | Email a supported PDF to the test mailbox, wait for the worker, and open the app. | One invoice record is created with sender, subject, received time, message ID, attachment ID, SharePoint link, and PDF. |
| UAT-05 | SharePoint intake | Place a PDF directly in the configured Incoming folder and wait for the worker scan. | One invoice record is created and routed into extraction. A second scan does not create another record. |
| UAT-06 | Search and history | Search a completed invoice by its six-digit IRJ. | Current status, stored metadata, PDF link, and chronological history are shown. |

## Extraction and review

| ID | Scenario | Steps | Expected result |
| --- | --- | --- | --- |
| UAT-07 | High-confidence extraction | Upload a clean digital PDF with all expected fields. | Company, supplier, supplier invoice number, date, value, currency, PO number, and confidence values are displayed. |
| UAT-08 | Ambiguous supplier | Upload Invoice 3 with a name that could match more than one supplier. | The invoice enters `Needs Review`; no supplier, approval route, or payment action is selected automatically. |
| UAT-09 | Unknown company | Upload an invoice addressed to an unconfigured company. | `Needs Review` includes a meaningful reason and the file remains recoverable. |
| UAT-10 | Unknown supplier | Upload an invoice for an unconfigured supplier. | `Needs Review` includes a meaningful reason and no approval is sent. |
| UAT-11 | Missing critical fields | Upload an invoice with no readable invoice number, date, or total. | `Needs Review` identifies the missing or low-confidence data. |
| UAT-12 | Incorrect extraction correction | Upload an invoice where one field is extracted incorrectly; correct it as Purchase Ledger and confirm routing. | Corrected fields persist, routing uses the corrected values, and the history identifies manual review. |
| UAT-13 | Supplier invoice-number format | Use a supplier with an invoice-number pattern; upload a number that violates it. | Confirmation is blocked or sent to review with a clear validation message. |
| UAT-14 | Supplier statement | Upload Statement 1. | It is classified as a statement, cannot enter invoice approval/payment workflow, and can be filed to the chosen company statement folder. |
| UAT-15 | Corrupt/renamed file | Upload each Invalid file. | It is rejected or safely moved to `Needs Review`; the worker continues processing later invoices. |
| UAT-16 | Scan quality | Upload a skewed, low-resolution, or partially obscured scan. | Fields are extracted only when supported by evidence; uncertain values require review. |
| UAT-17 | Unusual invoice layouts | Test credit note, utility bill, invoice with multiple totals, and invoice with a multi-line supplier address. | The correct document/type/total is selected or the invoice is routed to review; it never silently selects an unsafe value. |

## Routing, duplicates, and IRJ numbers

| ID | Scenario | Steps | Expected result |
| --- | --- | --- | --- |
| UAT-18 | PO query | On a PO invoice, record a price/quantity query with a purchasing contact. | It remains in the PO matching stage and folder. Query detail and contact are retained in history. |
| UAT-19 | Resolve PO query | Resolve the UAT-18 query by recording a match. | The prior query remains visible; only the explicit match advances the invoice. |
| UAT-20 | PO rejection | Reject an unmatched PO invoice with a reason. | It moves to `Rejected`; no further approval or payment action is possible. |
| UAT-21 | Duplicate invoice | Upload Invoice 1 again as Invoice 4. | It enters `Needs Review`, identifies the related invoice, and does not route automatically. |
| UAT-22 | False positive duplicate | Mark a suspected duplicate as genuinely separate and then route it. | The override is recorded and the new invoice can proceed. |
| UAT-23 | Confirmed duplicate | Confirm the duplicate is unwanted and cancel it. | Status becomes `Cancelled - Duplicate`; the original invoice is unchanged. |
| UAT-24 | Automatic IRJ | Route an invoice for a company configured for automatic IRJs. | A unique six-digit IRJ is assigned and cannot be changed during Sage registration. |
| UAT-25 | Manual IRJ | Route an invoice for a company configured for manual IRJs. Enter an existing IRJ, then a new six-digit IRJ. | The duplicate IRJ is rejected; the new valid IRJ is accepted and retained. |

## Approval, payment, and reconciliation

| ID | Scenario | Steps | Expected result |
| --- | --- | --- | --- |
| UAT-26 | Missing approval route | Process a nominal invoice for Supplier C through Sage registration. | It enters `Needs Review`, explains the missing route, and sends no approval email. |
| UAT-27 | Missing approver email | Remove an approver email from a test route and attempt Sage registration. | The invoice stops in `Needs Review`; it does not start an approval with an unverified address. |
| UAT-28 | Approval rejection | Reject a pending approval and include a reason. | Status becomes `Rejected`; the rejection reason and actor are visible; payment is blocked. |
| UAT-29 | Approval hold and resume | Put an approval on hold with a comment, then resolve it. | The invoice remains in the same approval stage/folder; the original and resolution comments remain in history. |
| UAT-30 | Notification idempotency | Hold/resume an approval or retry a page action. | The same stage email is not sent twice. |
| UAT-31 | Payment validation | Attempt payment before approval, then attempt payment without a date/reference. | Each invalid action is blocked with a clear message. |
| UAT-32 | Payment routes | Complete one BACS, Bankline, and Foreign POA payment route. | Each route moves the PDF to the correct folder and captures the chosen method and actor. |
| UAT-33 | Foreign allocation | Use the Foreign POA route, allocate payment, then reconcile. | Required allocation information is retained and the invoice reaches Complete only after reconciliation. |
| UAT-34 | Reconciliation validation | Try to reconcile an invoice that has not been marked paid. | The action is blocked. |

## Access, configuration, and resilience

| ID | Scenario | Steps | Expected result |
| --- | --- | --- | --- |
| UAT-35 | Role boundaries | Log in separately as Admin, Purchase Ledger, Approver 1, Approver 2, and Purchasing. | Each role sees and can perform only its authorised actions; an approver cannot decide the other approver’s stage. |
| UAT-36 | Admin configuration | Add/update a company, supplier alias, approval route, payment terms, confidence threshold, and IRJ mode. | Changes persist and affect later invoices without restarting the application. |
| UAT-37 | Malformed admin response | Disconnect PostgreSQL temporarily in the test environment, then open Admin. | The UI identifies the failed endpoint/status instead of showing an unhelpful JSON parsing error. |
| UAT-38 | Worker retry | Cause a temporary failure such as unavailable SharePoint during intake, restore it, and retry the queued item. | Failure is recorded; retry completes without a duplicate invoice or SharePoint file. |
| UAT-39 | Duplicate webhook delivery | Deliver or simulate the same Graph notification twice. | Only one queue item and one invoice are created. |
| UAT-40 | Restart recovery | Restart the web app and worker while an invoice is waiting at each major stage. | Records, status, audit history, and next permitted action remain intact after restart. |
| UAT-41 | Power BI data | After PostgreSQL connectivity is restored and migrations run, query `bi_invoice_metadata` and `bi_invoice_events` with the reporting reader. | One row exists per invoice, event rows join by invoice ID, and no write permission is available to the reporting reader. |
| UAT-42 | Pending approval reminders | Leave an Approver 1 and Approver 2 invoice pending and advance the test clock by 7 days twice. | Each assigned approver receives a reminder on each 7-day interval with a link to their own pending queue. |
| UAT-43 | On-hold reminders | Put an approval on hold and advance the test clock by 30 days twice. | The assigned approver receives a reminder on each 30-day interval; no 7-day reminder is sent while held. |
| UAT-44 | Rejection notification | Reject invoices from PO matching, approval, and flagged review. | Purchase Ledger receives one email per rejected invoice with the reason and a link to Rejected. |
| UAT-45 | Automatic payment routing | Configure BACS, Bankline, and Foreign POA defaults for three suppliers, then fully approve one invoice for each. | Each invoice moves automatically to its configured payment section and records the method and automatic actor. |
| UAT-46 | Payment routing issue | Fully approve an invoice with a missing, unsupported, or conflicting supplier payment setting. | The invoice enters Payment Routing Issue / On Hold with a clear reason; fixing the setting and retrying routes it automatically. |
| UAT-47 | Long invoice extraction | Submit a five-page invoice and inspect the mocked extraction request. | Only pages 1 and 5 are sent to Azure; the original full PDF remains in SharePoint. |
| UAT-48 | Duplicate-only deletion | Compare a normal incoming invoice with an invoice flagged as a possible duplicate. | Delete is hidden and rejected for the normal invoice; it is available for the possible duplicate. |
| UAT-42 | Extraction review display | Open a low-confidence invoice from the Purchase Ledger queue. | The PDF, extracted values, field confidence, warnings, editable corrections, and routing explanation are visible together. |
| UAT-43 | Correction feedback | Correct a supplier and invoice total, provide a correction reason, and confirm. | PostgreSQL retains the original values, corrected values, reason, reviewer, model version, prompt version, and final route; `bi_invoice_corrections` exposes the record. |
| UAT-44 | Worker visibility | Stop the worker until its heartbeat is stale, open Admin, then restart it. | Admin shows stale/down status, last heartbeat and queue counts; it returns to healthy after restart. |
| UAT-45 | Approval route editing | Edit approver names and email addresses for an existing company/supplier pair. | The existing PostgreSQL row is updated and no duplicate-entry error is shown. |
| UAT-46 | Today-only mailbox polling | With older unread invoice emails and one received today, start local polling with lookback `0`. | Only today's qualifying email is queued. Increasing the lookback deliberately includes older messages without duplicating an existing queue item. |
| UAT-47 | Multiple attachments and naming | Send one email with two PDF invoices. | Both PDFs create separate records. Final SharePoint names use `IRJ-original-name.pdf` and contain no `outlook-<hash>` prefix. |
| UAT-48 | Desktop launcher | Run `invoice-processor`, start services, open the web app, then stop services. | The launcher works on the target OS, reports both process states, opens the app, and shuts both child processes down cleanly. |
| UAT-49 | Individual approver isolation | Assign two invoices to different Approver 1 users and two to different Approver 2 users. Log in as each user and try the list, direct URL, PDF, search, activity feed, and approval endpoint for both invoices. | Each approver sees and can act only on invoices assigned to their own Entra email. Unassigned reads return 404 and unassigned decisions return 403. Admin retains authorised support access. |

## Exit criteria

The release is ready for pilot use when all core journeys pass, all high-risk
tests (UAT-08 through UAT-16, UAT-21, UAT-26 through UAT-31, and UAT-35 through
UAT-40) pass, no unexpected duplicate email or invoice is produced, and every
test invoice can be traced from source to its final SharePoint location and
audit history.
