# Custom-Code Invoice Processing Proposal

**Prepared:** 19 August 2026  
**Indicative volume:** approximately 10,000 invoices per year  
**Purpose:** Brief proposal for a custom web application integrated with the existing Microsoft 365 environment

## Executive recommendation

Build a custom internal web application hosted in Microsoft Azure, while continuing to use:

- Outlook as the invoice-receipt channel
- SharePoint as the document store
- Microsoft Entra ID for staff login
- Microsoft Graph for controlled Outlook and SharePoint access
- Azure AI Document Intelligence for invoice extraction

The custom application would provide the user interface, workflow rules, audit history, exception handling, approval routing and administration screens.

This is a hybrid design rather than a replacement for Microsoft 365. It retains Microsoft's identity, email and document-management services but avoids putting the complete business process into large, difficult-to-maintain Power Automate flows.

## Assumptions

The cost estimates in this proposal assume:

- Existing Microsoft 365 licences already provide Outlook, SharePoint and Entra ID.
- Approximately 10,000 invoices are processed each year.
- The average invoice is one to three pages.
- SharePoint remains the permanent PDF store.
- The application has a modest number of internal users rather than public access.
- Prices exclude VAT, one-off development, data migration and optional support contracts.
- Exact Azure prices depend on region, contract, exchange rate and any Microsoft/CSP discounts.

## 1. Overall design

### Proposed process

```text
Shared Outlook invoice mailbox
        ↓
Microsoft Graph email notification
        ↓
Background processing queue
        ↓
PDF saved to SharePoint Incoming Invoices
        ↓
Invoice record and audit entry created
        ↓
Azure AI Document Intelligence extracts invoice fields
        ↓
Purchase Ledger reviews the PDF and extracted information in the web app
        ↓
Purchase Ledger confirms or corrects the invoice
        ↓
Application applies the PO or nominal routing rules
        ↓
SharePoint file movement, notifications, approvals, payment and reconciliation
```

### Custom-built elements

- Browser-based invoice dashboard and PDF review screen
- Invoice register, statuses and audit history
- AI extraction orchestration and confidence validation
- Duplicate detection and exception queue
- PO matching and query process
- Nominal approval process
- Payment and reconciliation screens
- Rules engine for configurable routing and approval requirements
- Administration screens
- Background jobs, retries, reminders and escalation
- Power BI-ready reporting data

### Microsoft services retained

| Service | Proposed use |
| --- | --- |
| Outlook / Exchange Online | Receive invoice emails and send notifications |
| SharePoint | Store invoice PDFs in controlled company/process folders |
| Microsoft Graph | Access Outlook and SharePoint from the application |
| Microsoft Entra ID | Staff login, groups and role-based access |
| Azure | Host the application, database, queue, monitoring and AI service |
| Power BI | Optional management dashboard |

The production email trigger should use a Microsoft Graph change-notification webhook or controlled polling. MCP can be useful as an assistant/development interface, but it should not be the production event trigger.

## 2. Hosting

### Recommended hosting

Host the system in the organisation's Azure tenant:

- Web application/API: Azure App Service or Azure Container Apps
- Background processing: Azure Functions or a queue-driven worker
- Database: Azure SQL Database
- Queue: Azure Service Bus or Azure Storage Queue
- Secrets: Azure Key Vault
- Monitoring: Application Insights and Azure Monitor
- PDFs: existing SharePoint document libraries

Nothing would need to run on an employee's computer.

The web application must be available continuously for staff, but the invoice-processing workers can be event-driven and run only when required. A small scheduled process would also renew Microsoft Graph subscriptions and check for stalled invoices.

### Hosting choice

For the first production version, Azure App Service is the simplest operational choice. A serverless Azure Functions/Container Apps design could cost less, but introduces additional components. The final choice should be tested during the prototype and confirmed through the Azure Pricing Calculator.

## 3. AI service

### Recommendation: Azure AI Document Intelligence

Use the prebuilt invoice model in Azure AI Document Intelligence as the primary extraction service.

Reasons:

- It is designed specifically for invoices.
- It returns structured fields such as supplier, invoice number, dates, PO number, line items, subtotal, tax and total.
- It returns confidence scores that can drive the `Needs Review` queue.
- It can run in an Azure region selected by the organisation.
- It has predictable per-page pricing.
- It integrates directly with custom code without Power Platform licensing.

### Why not AI Builder?

AI Builder is convenient when the entire workflow is built in Power Apps and Power Automate, but it does not provide an extraction advantage for a custom-coded application. Microsoft is also moving AI Builder consumption from AI Builder credits to Copilot Credits during 2026, making future costs less straightforward for a new project.

### Why not use Claude as the primary extractor?

Claude could be useful for:

- Interpreting unusually formatted invoices
- Normalising ambiguous supplier or company names
- Extracting organisation-specific fields through flexible instructions
- Acting as a fallback when the specialist invoice model has low confidence

However, Claude is not recommended as the primary extractor because:

- It is a general-purpose model rather than a deterministic invoice service.
- Structured output and confidence handling require additional validation.
- Invoice data would be sent to another data processor and require a separate privacy, residency and contractual review.
- Model behaviour can change between versions.
- Azure Document Intelligence already covers the standard invoice fields required by this project.

A later hybrid option could send only low-confidence exceptions to an approved language model after a data-protection review.

## 4. Ongoing costs

### Indicative annual budget

The following figures are planning estimates, not supplier quotations.

| Component | Charging model | Indicative annual cost |
| --- | --- | ---: |
| Azure application hosting | Mostly fixed monthly | £900–£1,700 |
| Azure SQL database and included short-term backups | Mostly fixed monthly | £300–£800 |
| Queue, Key Vault and operational storage | Low usage-based | £25–£150 |
| Monitoring and logs | Usage-based | £60–£300 |
| Azure AI Document Intelligence | Per page | £75–£250 |
| Standard Microsoft Graph calls | Included with existing user licences within normal limits | £0 incremental |
| SharePoint PDF storage | Existing allocation assumed sufficient | £0 incremental |
| Entra ID basic SSO | Existing Microsoft 365 entitlement assumed | £0 incremental |
| GitHub Team, if not already purchased | Fixed per user | Approximately £40 per user/year |
| Optional long-term backup retention | Storage-based | £50–£250 |
| **Indicative total, excluding support and VAT** |  | **£1,500–£3,500/year** |

The expected central budget is approximately **£2,000–£2,500 per year excluding VAT**, assuming a modest production tier and two-page average invoices.

### Fixed versus variable costs

**Mainly fixed**

- Application hosting
- Database
- GitHub subscription
- Minimum monitoring configuration

**Volume-dependent**

- AI extraction pages
- Log volume
- Queue/API transactions
- Backup and archive storage
- Any optional external language-model usage

At 10,000 invoices per year, AI extraction is likely to be a small part of the total. Hosting, database and operational support are the larger costs.

### Other possible costs

- Existing Microsoft 365 licences are not included.
- Entra ID P1 may be required if advanced Conditional Access is not already licensed.
- Power BI licences may be required for dashboard users.
- An optional maintenance/support agreement would be separate. A basic retainer might be budgeted at approximately £250–£750 per month depending on response times and included development hours.
- Azure reservations or an existing Microsoft/CSP agreement may reduce the infrastructure figures.

## 5. Maintenance and future changes

Routine business configuration should be stored as data and maintained through an admin screen, not embedded in code.

### Changes authorised staff should be able to make

- Add, edit or deactivate companies
- Maintain company names, addresses, VAT numbers and aliases
- Add, edit or deactivate suppliers
- Maintain supplier aliases and company relationships
- Select first and second approvers
- Decide whether one or two approvals are required
- Maintain approval limits and value bands
- Maintain purchasing contacts
- Change notification recipients
- Change reminder and escalation periods
- Maintain SharePoint destination mappings
- Change AI confidence thresholds within approved limits
- Enable or disable optional workflow rules

All configuration changes should be permission-controlled and audited.

### Changes likely to require development

- Adding a new accounting-system integration
- Changing the fundamental invoice lifecycle
- Introducing a third or more complex approval stage
- Adding materially different document types
- Changing authentication or hosting architecture
- Introducing new external APIs
- Significant screen redesigns
- Complex new financial rules not supported by the configurable rules engine
- Changes required because Microsoft or another provider retires an API

The administration model should be designed during the first build, rather than added afterwards.

## 6. Ownership and documentation

Subject to the development contract, all bespoke source code, configuration templates and project documentation can belong to the organisation.

Recommended controls:

- Store source code in a private organisation-owned GitHub repository.
- Give at least two internal administrators owner access.
- Require pull-request review and protect the production branch.
- Keep Azure resources and subscriptions under the organisation's tenant.
- Keep domains, certificates, secrets and service accounts under organisational control.
- Record all third-party and open-source licences.

Required documentation should include:

- Architecture and data-flow diagrams
- Installation and deployment guide
- Environment and configuration guide
- Database schema and API documentation
- Administrator guide
- End-user guide
- Security and permissions model
- Monitoring and support runbook
- Backup and restore procedure
- Disaster-recovery procedure
- Test strategy and acceptance tests
- Known limitations and dependency register
- Change log and release procedure

## 7. Security and access

### User login

Staff should sign in with their existing Microsoft 365 accounts through Microsoft Entra ID. No separate application passwords should be created.

Access can be controlled through Entra groups and application roles, for example:

- Purchase Ledger
- Purchasing
- Approver
- Invoice Administrator
- Read-only Auditor

### Outlook security

- Use a dedicated Entra application or managed identity.
- Grant only the Graph permissions required.
- Restrict application mailbox access to the invoice shared mailbox.
- Do not grant tenant-wide mail access without an Exchange application-access restriction.
- Do not grant `Mail.Send` or `Mail.ReadWrite` until a confirmed feature requires it.

### SharePoint security

- Restrict application access to the relevant SharePoint site, preferably through `Sites.Selected` or an equivalent least-privilege configuration.
- Preserve SharePoint permissions, retention, versioning and audit features.
- Store secrets in Key Vault rather than source code or configuration files.
- Encrypt all browser and API traffic with HTTPS.

## 8. Reliability and support

The system should be designed so that an invoice cannot silently disappear.

Each invoice should have:

- A unique idempotency key based on the email and attachment
- A current status
- A last-successful processing stage
- A complete audit history
- A retry count and last error
- A link to the retained source PDF

Processing should use a queue and explicit workflow states. If Outlook, SharePoint, AI or another API fails:

1. Preserve the email/PDF reference and invoice record.
2. Retry temporary failures with controlled backoff.
3. Prevent duplicate records and duplicate approvals.
4. Move exhausted or non-retryable failures into `Needs Review`.
5. Display a clear reason and required action to administrators.
6. Alert support for critical or repeated failures.
7. Allow processing to resume from the failed stage.

The application should not mark an action as complete until the external service confirms success.

## 9. Backup and recovery

Recommended protection:

- Azure SQL automated backups and point-in-time restore
- Optional long-term retention for monthly or annual database backups
- SharePoint versioning, retention and recycle-bin protection for PDFs
- GitHub as the controlled source-code history
- Infrastructure-as-code templates so Azure resources can be recreated
- Key Vault backup and documented secret-rotation procedure
- Exportable business configuration
- Periodic restore testing

Recovery documentation should define:

- Recovery time objective: how quickly service should be restored
- Recovery point objective: how much recent data loss is acceptable
- Responsible people and escalation contacts
- Database restoration steps
- How to reconcile emails received while the application was unavailable

Because the mailbox and SharePoint documents remain in Microsoft 365, the application can reprocess outstanding emails after recovery using its idempotency controls.

## 10. Comparison with Power Automate

Power Automate could implement this specification, but two areas are likely to become restrictive as the process grows.

### Example 1: Exception handling and resumable processing

The specification includes unreadable PDFs, uncertain company or PO detection, duplicates, missing approvers, PO queries, rejected invoices and failed automation.

In Power Automate, handling every combination often produces large nested conditions and separate flows with difficult restart behaviour. Diagnosing which step succeeded before a failure can become time-consuming.

The custom solution would use:

- A database-backed state machine
- A durable queue
- Idempotency controls
- A visible exception queue
- Explicit retry and resume actions
- Structured logs and automated tests

### Example 2: Complex review and approval experience

Purchase Ledger needs to see the PDF beside extracted fields, correct uncertain values, confirm Sage registration, record PO discrepancies and understand the complete history.

Power Apps can provide this interface, but complex validation, permissions, performance and multi-stage workflow logic can become difficult to maintain across Power Apps, SharePoint lists and multiple flows.

The custom solution would provide one coherent interface and API with:

- Side-by-side PDF review
- Field-level validation and confidence warnings
- Configurable approval rules
- Role-based actions
- Immediate status history
- Automated unit, integration and end-to-end tests

This does not mean Power Automate is incapable of these tasks. The custom option offers greater control, testability and maintainability when the workflow becomes business-critical and exception-heavy.

## Proposed delivery approach

1. Build an isolated prototype using sample documents and no production writes.
2. Confirm the SharePoint structure, configuration model and status lifecycle.
3. Connect read-only Outlook and SharePoint access.
4. Add invoice ingestion and idempotent storage.
5. Add Azure AI Document Intelligence.
6. Add Purchase Ledger review and confirmation.
7. Add PO and nominal routing.
8. Add approvals, payment and reconciliation.
9. Complete security, recovery, monitoring and user-acceptance testing.
10. Enable production access through a controlled release.

## Sources and pricing references

- [Azure AI Document Intelligence pricing](https://azure.microsoft.com/en-gb/pricing/details/document-intelligence/)
- [Azure Functions pricing](https://azure.microsoft.com/en-gb/pricing/details/functions/)
- [Azure App Service pricing](https://azure.microsoft.com/en-gb/pricing/details/app-service/linux/)
- [Azure SQL Database pricing](https://azure.microsoft.com/en-gb/pricing/details/azure-sql-database/single/)
- [Azure Monitor pricing](https://azure.microsoft.com/en-gb/pricing/details/monitor/)
- [Azure Key Vault pricing](https://azure.microsoft.com/en-gb/pricing/details/key-vault/)
- [Microsoft Graph metered API overview](https://learn.microsoft.com/en-us/graph/metered-api-overview)
- [Microsoft AI Builder credit transition](https://learn.microsoft.com/en-us/ai-builder/endofaibcredits)
- [Microsoft Entra licensing](https://learn.microsoft.com/en-us/entra/fundamentals/licensing)
- [Anthropic API pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [GitHub pricing](https://github.com/pricing)

All costs should be reconfirmed in the Azure Pricing Calculator and with the organisation's Microsoft licensing provider before approval.
