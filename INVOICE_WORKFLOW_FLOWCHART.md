# Invoice Processing Workflow Flowchart

This flowchart represents the original end-to-end invoice logic, including:

- Outlook and manual invoice intake
- IRJ generation after Purchase Ledger confirmation (or at Sage for companies
  configured for manual IRJ numbering)
- AI extraction, validation, review, and duplicate checks
- Purchase Order matching and query resolution
- Nominal invoice registration and one/two-level approval routing
- Payment recording and bank reconciliation
- Safe exception handling without automatic guessing

```mermaid
flowchart TD
    A([Invoice received]) --> B{Source}
    B -->|Shared Outlook mailbox| C[Monitor inbox and download PDF attachments]
    B -->|Manual upload| D[Purchase Ledger uploads PDF]
    C --> E[Save original PDF and create invoice register record]
    D --> E
    E --> G[AI extracts required invoice fields and confidence]

    G --> H{Readable, required fields present,<br/>and confidence acceptable?}
    H -->|No| NR[Needs Review]
    NR --> NR1[Purchase Ledger reviews and corrects details]
    NR1 --> CONF[Purchase Ledger confirms corrected details]
    H -->|Yes| DUP{Possible duplicate?}
    DUP -->|Yes| NR2[Purchase Ledger reviews duplicate]
    NR2 -->|Legitimate invoice| CONF
    NR2 -->|Confirmed duplicate| STOP1([Stop / cancel])
    DUP -->|No| CONF
    CONF --> F[Assign automatic IRJ,<br/>or defer manual IRJ until Sage]
    F --> R{PO number present?}

    R -->|Yes - PO invoice| PO1[Prefix filename with IRJ<br/>Move to company PO Matching folder]
    PO1 --> PO2[Notify Purchase Ledger once]
    PO2 --> PO3[Purchase Ledger manually compares invoice,<br/>PO and goods received]
    PO3 --> PO4{PO matches?}
    PO4 -->|No| POQ[Record query without changing stage or folder]
    POQ --> POQ1[Record category, notes and purchasing contact]
    POQ1 --> POQ2[Purchasing investigates and PL records resolution]
    POQ2 -->|No repeat stage email| PO3
    PO4 -->|Yes| PO5[Purchase Ledger registers invoice in Sage<br/>and confirms Matched / Registered]
    PO5 --> APPR

    R -->|No - nominal invoice| N1[Prefix filename with IRJ<br/>Move to company nominal area]
    N1 --> N2[Purchase Ledger registers invoice in Sage]
    N2 --> N3{Company + supplier approval route found?}
    N3 -->|No| NR3[Needs Review: configure approver route]
    NR3 --> N3
    N3 -->|Yes| A1[Send one request to Approver 1]
    A1 --> A1D{Approver 1 decision}
    A1D -->|Reject| REJ([Rejected])
    A1D -->|Query / On Hold| HOLD1[Record query; remain at Approval 1]
    HOLD1 --> HOLD1R[Purchase Ledger resolves query and resumes]
    HOLD1R -->|No repeat stage email| A1D
    A1D -->|Approve| A2Q{Second approval required?}
    A2Q -->|No| APPR
    A2Q -->|Yes| A2[Send one request to Approver 2]
    A2 --> A2D{Approver 2 decision}
    A2D -->|Reject| REJ
    A2D -->|Query / On Hold| HOLD2[Record query; remain at Approval 2]
    HOLD2 --> HOLD2R[Purchase Ledger resolves query and resumes]
    HOLD2R -->|No repeat stage email| A2D
    A2D -->|Approve| APPR

    APPR[Approved] --> APPR1[Notify Purchase Ledger once<br/>Show in Approved for Payment]
    APPR1 --> ROUTE{Choose payment route}
    ROUTE -->|BACS / Bankline| PAY[Purchase Ledger deliberately records payment<br/>date, method and reference]
    ROUTE -->|Foreign POA| ALLOC[Record foreign payment allocation]
    ALLOC --> PAY
    PAY --> PAID[Paid / Awaiting Bank Reconciliation]
    PAID --> REC[Purchase Ledger deliberately confirms reconciliation<br/>and records date / notes]
    REC --> DONE([Reconciled / Complete])

    classDef automated fill:#dbeafe,stroke:#2563eb,color:#172554;
    classDef human fill:#fef3c7,stroke:#d97706,color:#451a03;
    classDef decision fill:#ede9fe,stroke:#7c3aed,color:#2e1065;
    classDef exception fill:#fee2e2,stroke:#dc2626,color:#450a0a;
    classDef complete fill:#dcfce7,stroke:#16a34a,color:#052e16;

    class C,E,F,G,PO1,PO2,N1,A1,A2,APPR1 automated;
    class D,NR1,NR2,CONF,PO3,POQ1,POQ2,PO5,N2,HOLD1R,HOLD2R,ALLOC,PAY,REC human;
    class B,H,DUP,R,PO4,N3,A1D,A2Q,A2D,ROUTE decision;
    class NR,NR3,POQ,HOLD1,HOLD2,REJ,STOP1 exception;
    class A,APPR,PAID,DONE complete;
```

## Legend

- **Blue:** automated system action
- **Yellow:** manual human action
- **Purple:** decision
- **Red:** exception, hold, rejection, or cancellation
- **Green:** major lifecycle state

The editable Mermaid-only source is available in `INVOICE_WORKFLOW_FLOWCHART.mmd`.

## Implementation confirmation

| Flow area | Implemented by | Verified behavior |
|---|---|---|
| Outlook and manual intake | `app/outlook_worker.py`, `app/sharepoint_intake.py`, `app/main.py` | Both routes create the same invoice record and extraction stage. |
| Extraction and review | `InvoiceLifecycle.run_extraction`, `confirm_and_route` | Unreadable, incomplete, low-confidence, unknown-company, and unknown-supplier results stop for Purchase Ledger review; no value is guessed. |
| Duplicate decision | `app/duplicates.py`, `confirm_and_route`, `cancel_confirmed_duplicate` | Possible duplicates stop for review and can only continue after explicit override or be cancelled. |
| PO flow | `confirm_and_route`, `record_po_match`, `register_in_sage` | PO queries preserve the PO-matching stage and folder; only explicit match advances to Sage and approval. |
| Nominal flow | `register_in_sage`, `decide_approval`, `resume_approval` | Missing routes stop for configuration; approvals are sequential; query/hold metadata never advances or moves the invoice. |
| Stage notifications | `InvoiceLifecycle._send_stage_email_once` | PO matching, Approver 1, Approver 2, and approved-for-payment each have one persistent email claim per invoice; query/resume loops cannot resend them. |
| Payment and reconciliation | `route_for_payment`, `mark_paid`, `mark_foreign_allocated`, `mark_reconciled` | Payment routing, deliberate payment recording, and deliberate reconciliation are separate guarded transitions. |
| Audit and lookup | `ActivityFeedStore`, `PostgresActivityFeedStore`, `/api/invoice-search*` | IRJ search shows current status and the chronological audit events for that invoice. |
