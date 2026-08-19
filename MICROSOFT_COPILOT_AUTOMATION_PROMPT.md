# Microsoft Copilot Automation Prompt

Copy the text below into Microsoft Copilot. This request is for a safe prototype only. Values inside square brackets are placeholders and must remain placeholders until a later implementation is explicitly approved.

---

I want you to help me design and demonstrate a working prototype of an automated invoice-processing solution using the intended behaviour of Power Automate, SharePoint, Outlook, AI Builder, Microsoft Approvals, and Power Apps.

The prototype must demonstrate the user interface, extracted invoice fields, routing decisions, status changes, and intended flow logic without changing or connecting to the live Microsoft 365 environment.

## Prototype-only safety boundary

Do not create, edit, delete, move, rename, upload, or overwrite anything in a live SharePoint site.

Do not monitor a live mailbox, send real emails, create real approvals, call Sage, use production connectors, contact real users, or enable scheduled or automatic flows.

Do not create production SharePoint lists, libraries, folders, permissions, Power Apps, Power Automate flows, AI Builder models, connections, connection references, environment variables, or Dataverse tables.

Instead:

1. Produce the proposed architecture and detailed flow definitions.
2. Create a mock or isolated prototype using sample invoice data and placeholder identities.
3. Use in-memory Power Apps collections, static sample records, local sample PDFs, or equivalent mock data where possible.
4. Represent email, AI extraction, approvals, SharePoint file moves, Sage registration, payment, and reconciliation as simulated actions.
5. Display a visible `PROTOTYPE - NO LIVE ACTION` label throughout the interface.
6. Use manual prototype buttons rather than automatic email, file, or scheduled triggers.
7. Record simulated actions in a mock history collection rather than a live audit list.
8. Present any Power Automate flows as disabled drafts, diagrams, pseudocode, or action-by-action build instructions.
9. Require a separate explicit approval before producing deployment instructions or connecting any live service.

If Microsoft Copilot cannot create a prototype without modifying the tenant, stop and provide the design, mock data, screen definitions, formulas, flow diagrams, and build instructions only.

The future production solution should use several focused flows with clear triggers and responsibilities rather than one very large flow. SharePoint would become the source of truth only after implementation is approved.

## What the Invoice Register means

The Invoice Register is not an accounting system and does not post anything to Sage. It is a proposed tracking table containing one record for each invoice.

In the prototype, represent it as a mock data collection. Each row should contain the invoice's IRJ, extracted information, current status, PDF reference, processing dates, decisions, and other tracking fields. This allows the prototype application to show where every invoice is in the process.

In a later approved implementation, this mock collection could become a SharePoint list named using `[INVOICE REGISTER LIST NAME]`. Do not create that list during the prototype stage.

Use the following unresolved production placeholders only to show what would be configured later:

- SharePoint site: `[SHAREPOINT SITE URL]`
- Shared invoice mailbox: `[INVOICE MAILBOX ADDRESS]`
- Purchase Ledger notification email or group: `[PURCHASE LEDGER EMAIL OR GROUP]`
- Purchasing notification email or group: `[PURCHASING EMAIL OR GROUP]`
- Automation support email or group: `[AUTOMATION SUPPORT EMAIL OR GROUP]`
- SharePoint invoice document library: `[DOCUMENT LIBRARY NAME]`
- SharePoint Incoming Invoices folder: `[INCOMING INVOICES FOLDER]`
- Company PO matching subfolder: `[PO MATCHING FOLDER]`
- Company nominal invoice subfolder: `[NOMINAL INVOICES FOLDER]`
- Company approved subfolder: `[APPROVED FOLDER]`
- Company bank reconciliation subfolder: `[BANK RECONCILIATION FOLDER]`
- Company completed-invoice subfolder: `[COMPLETE FILED FOLDER]`
- Companies configuration list: `[COMPANIES LIST NAME]`
- Suppliers configuration list: `[SUPPLIERS LIST NAME]`
- Approval matrix list: `[APPROVAL MATRIX LIST NAME]`
- Invoice register list: `[INVOICE REGISTER LIST NAME]`
- Invoice history list: `[INVOICE HISTORY LIST NAME]`
- IRJ number format: `[IRJ NUMBER FORMAT]`
- AI confidence threshold: `[AI CONFIDENCE THRESHOLD]`
- PO reminder period: `[PO REMINDER DAYS]`
- Sage-registration reminder period: `[SAGE REGISTRATION REMINDER DAYS]`
- Approval reminder period: `[APPROVAL REMINDER DAYS]`
- Reconciliation reminder period: `[RECONCILIATION REMINDER DAYS]`

## Mandatory placeholder rule

Do not invent, assume, or hard-code any environment-specific value.

Whenever the automation needs a mailbox, email recipient, person, Microsoft 365 group, SharePoint site, document library, folder, list, user ID, approval owner, URL, confidence threshold, reminder period, escalation period, or similar tenant-specific value, use a clearly named square-bracket placeholder.

Examples include:

- `[PURCHASE LEDGER EMAIL OR GROUP]`
- `[PURCHASING EMAIL OR GROUP]`
- `[AUTOMATION SUPPORT EMAIL OR GROUP]`
- `[INCOMING INVOICES FOLDER]`
- `[PO MATCHING FOLDER]`
- `[COMPANY FOLDER PATH]`
- `[APPROVER FROM APPROVAL MATRIX]`
- `[INVOICE REGISTER LIST NAME]`

If a required value is not listed in this prompt, create a new descriptive placeholder such as `[REPLACE WITH ...]` and tell me what must replace it. Do not substitute example addresses, sample users, guessed folder paths, or default tenant values.

## Overall business process

An invoice normally arrives as a PDF attachment in the shared invoice mailbox. A member of Purchase Ledger must also be able to upload a PDF manually and start the same process.

The original PDF must first be saved in the `[INCOMING INVOICES FOLDER]` SharePoint folder. After it has been saved, create an Invoice Register record, generate a unique IRJ reference, extract the invoice information with AI Builder, and display the PDF and extracted information in a user-facing application for Purchase Ledger.

The IRJ reference must be unique and must never be reused. Generate it from the SharePoint Invoice Register item ID using `[IRJ NUMBER FORMAT]`. An example format could be `IRJ-001245`, but do not use that example unless it is confirmed. Store the IRJ as SharePoint metadata immediately, but do not rename the PDF yet. Display the IRJ in the application so Purchase Ledger can use it when registering the invoice in Sage.

Purchase Ledger must be able to review and correct the extracted information. Automation must never guess when important information is missing, uncertain, or invalid. In those circumstances, change the invoice status to `Needs Review`, record the specific reason, and stop automatic processing until an authorised user corrects and confirms the information.

After extraction and validation, use the Purchase Order number to choose between the Purchase Order route and the nominal invoice route.

## SharePoint information

Create a mock Invoice Register collection for the prototype with one sample record for every sample invoice. Do not create or use a live SharePoint list. Model at least the following fields:

- IRJ number
- Current status
- Invoice type, either PO or Nominal
- Company
- Supplier
- Supplier invoice number
- Purchase Order number
- Invoice date
- Net value
- VAT value
- Total invoice value
- Currency
- Date received
- Source, either Email or Manual
- Original filename
- Current filename
- SharePoint PDF link
- Original email sender
- Original email subject
- Original email received date
- Outlook message ID
- AI extraction confidence or review result
- Review reason
- Approver 1
- Approver 1 decision, date, and comments
- Approver 2
- Approver 2 decision, date, and comments
- PO matching status
- PO query category and notes
- Sage registration status
- Registered by
- Sage registration date
- Payment date
- Payment reference and notes
- Reconciliation date
- Reconciliation user and notes
- Last flow name
- Last flow run ID
- Last error

Create a mock Invoice History collection whenever a simulated important event occurs. Each mock history entry must contain the IRJ, invoice record ID, event type, previous status, new status, date and time, placeholder user or simulated process, comments, and any related mock flow-run or approval identifier.

Use mock Companies data to demonstrate mapping extracted company names, addresses, VAT numbers, and aliases to placeholder SharePoint company folders.

Use mock Suppliers data to demonstrate normalised supplier information and aliases.

Use a mock Approval Matrix collection that maps a sample company and supplier to placeholder Approver 1, whether a second approval is required, and placeholder Approver 2.

## Required prototype screens

Create or describe a clickable prototype with:

1. A dashboard showing sample invoices grouped by status.
2. An Incoming Invoices queue.
3. An invoice-detail screen showing a sample PDF preview and extracted fields.
4. An edit/review mode for correcting simulated AI extraction.
5. A PO matching screen with `Matched` and `Query` prototype actions.
6. A nominal Sage-registration screen with a `Registered` prototype action.
7. An approval screen for simulated Approver 1 and Approver 2 decisions.
8. Payment and reconciliation prototype forms.
9. An audit-history panel.
10. A clear banner stating that all actions are simulated.

Use sample data covering at least:

- One valid PO invoice
- One PO invoice with a matching query
- One nominal invoice requiring one approval
- One nominal invoice requiring two approvals
- One low-confidence invoice requiring review
- One possible duplicate invoice

## Proposed production Flow 1: Email invoice intake

For the prototype, simulate this flow with a manual button and a sample email/attachment record. Do not connect to or monitor `[INVOICE MAILBOX ADDRESS]`.

The future production flow would be triggered when a new email arrives in the shared invoice mailbox.

The flow must:

1. Record the sender, subject, received date, Outlook message ID, and original attachment name.
2. Inspect all attachments.
3. Process each supported PDF attachment as a separate invoice.
4. Save the original PDF into the SharePoint `[INCOMING INVOICES FOLDER]` folder without changing its contents.
5. Avoid silently overwriting an existing file. If a filename collision occurs, create a safe unique temporary filename and retain the original filename as metadata.
6. Create the Invoice Register item and set its status to `Incoming / Processing`.
7. Generate the unique IRJ from the Invoice Register item ID.
8. Store the IRJ and SharePoint PDF information on the register item.
9. Create an Invoice History entry for receipt, file storage, and IRJ generation.
10. Start the extraction and routing flow.

If the attachment cannot be saved or the register record cannot be created, record the failure and notify `[PURCHASE LEDGER EMAIL OR GROUP]` and, where appropriate, `[AUTOMATION SUPPORT EMAIL OR GROUP]`. Do not report the invoice as successfully processed.

## Proposed production Flow 2: Manual invoice intake

Create a separate flow for a PDF uploaded manually by Purchase Ledger.

The manual flow must save the PDF in `[INCOMING INVOICES FOLDER]`, create the same Invoice Register information, generate the IRJ, and start the same extraction and routing process as the email flow.

Set the source to `Manual`. Do not require email metadata for manually uploaded invoices.

## Proposed production Flow 3: AI extraction and validation

Run this flow after the PDF and Invoice Register record exist.

Use AI Builder invoice processing to extract, where available:

- Company being invoiced
- Supplier name
- Supplier invoice number
- Purchase Order number
- Invoice date
- Net value
- VAT value
- Total invoice value
- Currency
- Company address or VAT number where useful for company identification

Save the extracted values and available confidence scores to the Invoice Register.

Validate that:

1. AI Builder successfully read the PDF.
2. The company can be mapped confidently to an active Companies-list record.
3. The supplier can be identified.
4. The supplier invoice number is present.
5. The invoice date and total value are valid.
6. The Purchase Order number is either confidently present or confidently absent.
7. Critical confidence scores meet `[AI CONFIDENCE THRESHOLD]`.

Also check for a possible duplicate using company, supplier, and supplier invoice number as the primary key. Use invoice date and value as supporting evidence.

If the PDF is unreadable, a required value is missing, the confidence is too low, the company or supplier cannot be mapped, the PO result is unclear, or a possible duplicate exists:

1. Set the status to `Needs Review`.
2. Record a clear review reason.
3. Add an Invoice History entry.
4. Display the invoice in the Purchase Ledger review queue.
5. Stop processing until an authorised user corrects and confirms it.

When an authorised user completes the review, rerun validation and continue from the routing decision. Do not create a second Invoice Register item or a second PDF.

## User-facing application

Create a Power Apps interface, or expose the required data so a Power Apps interface can be created, for Purchase Ledger.

The application must show:

- A preview or secure link to the PDF
- IRJ number
- Current status
- All extracted invoice information
- Validation warnings and review reasons
- Purchase Order number
- Current SharePoint folder
- Approval, matching, payment, and reconciliation history
- Actions available to the signed-in user

Purchase Ledger must be able to correct extracted values, confirm a manual review, record PO matching results, confirm Sage registration, record payment, and record reconciliation.

All actions must update the existing Invoice Register record and call controlled Power Automate flows. Do not create a separate application database.

## Routing decision

After successful validation:

- If a Purchase Order number is present, set the invoice type to `PO` and start the Purchase Order route.
- If no Purchase Order number is present, set the invoice type to `Nominal`, set the status to `Awaiting Sage Registration`, and display it in the nominal Sage-registration queue.
- If the presence of a PO number is uncertain, send the invoice to `Needs Review`.

## Proposed production Flow 4: Purchase Order routing and notification

When a valid PO invoice is detected:

1. Set the status to `Awaiting PO Matching`.
2. Find the correct company using the Companies list.
3. Move the same PDF from `[INCOMING INVOICES FOLDER]` into the matched company's `[PO MATCHING FOLDER]`.
4. Do not create an uncontrolled duplicate copy.
5. Update the Invoice Register with the new SharePoint location and PDF link.
6. Preserve all metadata and the IRJ.
7. Add an Invoice History entry for the routing and file move.
8. Email `[PURCHASE LEDGER EMAIL OR GROUP]` to say that a PO invoice requires matching.

The email must include:

- IRJ number
- Company
- Supplier
- Supplier invoice number
- Purchase Order number
- Invoice date
- Net, VAT, and total values
- Secure SharePoint PDF link
- Link to the invoice record or application matching screen

If the destination folder cannot be found or the move fails, keep the PDF safely accessible, set the invoice to `Needs Review`, record the error, and notify Purchase Ledger. Never mark a failed move as successful.

## Proposed production Flow 5: Purchase Order matching

Purchase Ledger must manually compare the PO invoice with:

- The Purchase Order
- Goods-received information
- Price and quantity information
- Any other relevant purchasing records

The application must allow Purchase Ledger to choose either `Matched` or `Query`.

If Purchase Ledger selects `Query`, require:

- Query category, such as price difference, quantity difference, goods not received, partial delivery, or other
- Query details
- Relevant Purchasing contact
- Date raised

Set the status to `PO Query / Matching Issue`. Keep the PDF in `[PO MATCHING FOLDER]`. Notify the Purchasing contact selected from the configured company or supplier record. If no contact is configured, use `[PURCHASING EMAIL OR GROUP]` and flag the missing configuration. Preserve the query in Invoice History.

The invoice must remain outstanding and must not proceed to approval or payment while the query is unresolved.

When the query is resolved, require Purchase Ledger to record the resolution notes and repeat the matching decision.

If Purchase Ledger confirms that the invoice matches:

1. Display the extracted invoice information and IRJ for manual registration in Sage.
2. Require Purchase Ledger to register the invoice in Sage.
3. Require Purchase Ledger to click `Matched / Registered`.
4. Validate that the required invoice fields and IRJ are present.
5. Record who completed the matching and Sage registration and when.
6. Prefix the PDF filename using `{IRJ}_{original-filename}.pdf`.
7. Move the PDF into the relevant company's `[APPROVED FOLDER]`.
8. Update the current filename, location, PDF link, matching result, registration result, and status.
9. Set the status to `Approved`.
10. Add Invoice History entries for matching, Sage registration, renaming, movement, and approval.

If the rename or move fails, do not set the status to `Approved`. Retain the PDF, set a clear error or review status, and notify Purchase Ledger.

## Proposed production Flow 6: Nominal Sage registration

If no PO number is present:

1. Display the invoice PDF, extracted information, and IRJ to Purchase Ledger.
2. Allow Purchase Ledger to correct the information if necessary.
3. Require Purchase Ledger to register the invoice manually in Sage using the displayed IRJ.
4. Require Purchase Ledger to click `Registered`.
5. Validate that all required fields and the IRJ are present.
6. Record who registered the invoice and when.
7. Prefix the PDF filename using `{IRJ}_{original-filename}.pdf`.
8. Move the same PDF from `[INCOMING INVOICES FOLDER]` into the relevant company's `[NOMINAL INVOICES FOLDER]`.
9. Update the Invoice Register with the filename, location, PDF link, and Sage registration details.
10. Add Invoice History entries for registration, renaming, and movement.
11. Start the nominal approval flow.

If the company folder cannot be found or the rename or move fails, do not start approval. Keep the PDF safely accessible, set the status to `Needs Review`, record the error, and notify Purchase Ledger.

## Proposed production Flow 7: Nominal invoice approval

For a registered nominal invoice, look up the active Approval Matrix record using company and supplier.

If no valid approval route exists, set the invoice to `Needs Review`, record the reason, and stop.

If a valid route exists:

1. Store Approver 1 and optional Approver 2 on the Invoice Register.
2. Set the status to `Awaiting Approval 1`.
3. Create a Microsoft Approval for Approver 1.
4. Include the IRJ, company, supplier, supplier invoice number, invoice date, values, and secure PDF link.
5. Record the approval request ID.

Approver 1 must be able to approve or reject and provide comments. Record the user, decision, date, and comments.

If Approver 1 rejects the invoice, require a reason, set the status to `Rejected`, notify Purchase Ledger, and stop the normal workflow.

If Approver 1 approves and no second approval is required, set the invoice to `Approved`.

If Approver 1 approves and a second approval is required:

1. Set the status to `Awaiting Approval 2`.
2. Create a separate approval for Approver 2.
3. Record Approver 2's user, decision, date, and comments.

If Approver 2 approves, set the invoice to `Approved`.

If Approver 2 rejects, require a reason, set the status to `Rejected`, notify Purchase Ledger, and stop.

Allow an approver to record that the invoice is on hold or under query, including a reason such as price under query, goods or services disputed, further information required, or waiting for a credit note. Set the status to `Approval Query / On Hold` and define a controlled action for resuming the approval after the issue is resolved.

Whenever a nominal invoice becomes fully approved:

1. Record how it became approved.
2. Add an Invoice History entry.
3. Notify Purchase Ledger.
4. Move the PDF into the relevant company's `[APPROVED FOLDER]` if it is not already there.
5. Update its SharePoint location and link.

## Proposed production Flow 8: Payment

An approved invoice must not be marked as paid automatically.

Allow only authorised Purchase Ledger users to deliberately record:

- Payment date
- Payment reference, BACS reference, payment-run reference, cheque reference, or other payment information
- Optional payment notes

When Purchase Ledger confirms payment:

1. Validate that the invoice status is `Approved`.
2. Record who marked it as paid and when.
3. Set the status to `Paid / Awaiting Bank Reconciliation`.
4. Move the PDF into the relevant company's `[BANK RECONCILIATION FOLDER]`.
5. Update the Invoice Register location and PDF link.
6. Add an Invoice History entry.

If the move fails, record the failure and do not falsely report that the complete payment transition succeeded.

## Proposed production Flow 9: Bank reconciliation

Allow only authorised Purchase Ledger users to deliberately record:

- Reconciliation date
- Reconciliation notes

When reconciliation is confirmed:

1. Validate that the invoice status is `Paid / Awaiting Bank Reconciliation`.
2. Record who completed the reconciliation and when.
3. Set the status to `Reconciled / Complete`.
4. Move the PDF into the relevant company's `[COMPLETE FILED FOLDER]`.
5. Update the Invoice Register location and PDF link.
6. Add an Invoice History entry.

## Statuses

Use these controlled status values:

- `Incoming / Processing`
- `Needs Review`
- `Awaiting Sage Registration`
- `Awaiting PO Matching`
- `PO Query / Matching Issue`
- `Awaiting Approval 1`
- `Awaiting Approval 2`
- `Approval Query / On Hold`
- `Rejected`
- `Approved`
- `Paid / Awaiting Bank Reconciliation`
- `Reconciled / Complete`

Do not allow invalid status changes. Validate the current status before every user or automated transition.

## Error handling

Use Power Automate scopes for the main actions and failure handling. Configure safe retry policies only where retrying cannot create duplicate records, approvals, emails, or file moves.

For every failure:

1. Preserve the existing Invoice Register record and canonical PDF.
2. Record the flow name, run ID, timestamp, and useful error message.
3. Add an Invoice History entry.
4. Set the invoice to `Needs Review` when human intervention is required.
5. Notify the appropriate owner for critical failures.
6. Provide an authorised restart action that continues from the correct stage without creating a duplicate invoice.

Handle at least:

- Unsupported or unreadable PDF
- AI Builder failure or low confidence
- Unknown company
- Unknown supplier
- Unclear PO result
- Possible duplicate invoice
- Missing approval-matrix entry
- Missing approver
- SharePoint filename collision
- Missing destination folder
- File rename or move failure
- Outlook notification failure
- Approval creation failure
- Power Automate connector failure
- User attempting an invalid action for the current status

## Security and permissions

Use Microsoft Entra ID identities and Microsoft 365 permissions.

Apply least privilege:

- Purchase Ledger can review extracted information, complete matching, confirm Sage registration, record payment, and record reconciliation.
- Purchasing can view and respond to assigned PO queries.
- Approvers can view and decide only the invoices assigned to them.
- Authorised administrators can maintain Companies, Suppliers, and Approval Matrix records.
- Unauthorised users must not be able to approve, register, pay, reconcile, or change protected invoice fields.

Do not place passwords, tokens, or secret values in SharePoint lists, flow definitions, emails, or this prompt.

## Monitoring and reminders

Create a scheduled monitoring flow that identifies:

- Invoices stuck in `Incoming / Processing`
- Items waiting for manual review
- PO matches or queries outstanding beyond `[PO REMINDER DAYS]`
- Sage registrations outstanding beyond `[SAGE REGISTRATION REMINDER DAYS]`
- Approvals outstanding beyond `[APPROVAL REMINDER DAYS]`
- Paid invoices awaiting reconciliation beyond `[RECONCILIATION REMINDER DAYS]`
- Recent automation failures

Send reminders and escalations to the configured owners without creating duplicate approval requests.

## SharePoint operational views

Create or support these views:

- Incoming / Processing
- Needs Review
- Awaiting Sage Registration
- Awaiting PO Matching
- PO Queries
- Awaiting Approval 1
- Awaiting Approval 2
- Approval Queries / On Hold
- Rejected
- Approved for Payment
- Awaiting Bank Reconciliation
- Complete
- My Outstanding Invoices

## Prototype build order

Build and test only the isolated mock prototype in this order:

1. Define mock collections for invoices, companies, suppliers, approval rules, and history.
2. Create the prototype dashboard, invoice queues, detail screen, and sample PDF preview.
3. Simulate IRJ generation and AI extraction with sample values and confidence results.
4. Simulate manual review, duplicate detection, and corrected values.
5. Simulate PO routing, notification, matching, and query handling without moving or emailing anything.
6. Simulate nominal Sage registration and sequential approvals with placeholder users.
7. Simulate payment and reconciliation transitions.
8. Demonstrate audit history, errors, reminders, and operational reporting.
9. Produce a separate future-production build plan for SharePoint, Power Automate, AI Builder, Outlook, Approvals, and Power BI.

For each proposed production flow, provide documentation only:

- The exact trigger
- Required connections
- Every action in execution order
- Conditions and expressions
- SharePoint columns used
- Status changes
- Error-handling scopes
- Required permissions
- Test cases

Before designing the prototype or documenting future flows, identify any missing environment values or business decisions. Leave every missing value as a descriptive square-bracket placeholder and provide a checklist explaining what must replace each placeholder. Do not invent mailbox addresses, email recipients, SharePoint URLs, document libraries, folder names, users, groups, confidence thresholds, reminder periods, escalation rules, or approval rules.

Do not produce deployment or activation steps unless I later provide explicit approval to move beyond the prototype.

---

## Important usage note

If Copilot asks you to narrow the request, begin with the **Required prototype screens** and **Prototype build order** sections. Use the proposed production-flow sections only to define simulated behaviour. Do not paste a flow section into a production connector or enable a live trigger.
