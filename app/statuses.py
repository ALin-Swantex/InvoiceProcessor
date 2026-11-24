"""Every invoice status, in workflow order.

The values are stored in the database and shown in the UI (app/static/app.js
uses the same strings), so they must never change.
"""

from __future__ import annotations


class Status:
    AWAITING_EXTRACTION = "Awaiting AI Extraction"
    NEEDS_REVIEW = "Needs Review"
    # Held while confirm_and_route works on an invoice, so a second confirm
    # cannot allocate another IRJ number.
    PROCESSING_CONFIRMATION = "Processing Confirmation"

    AWAITING_PO_MATCHING = "Awaiting PO Matching"
    PO_QUERY = "PO Query / Matching Issue"
    AWAITING_SAGE_REGISTRATION = "Awaiting Sage Registration"

    AWAITING_APPROVAL_1 = "Awaiting Approval 1"
    AWAITING_APPROVAL_2 = "Awaiting Approval 2"
    WORKFLOW_ISSUE = "Workflow Issue / On Hold"

    APPROVED = "Approved"
    PAYMENT_ROUTING_ISSUE = "Payment Routing Issue / On Hold"
    APPROVED_BACS = "Approved for Payment - BACS"
    APPROVED_BANKLINE = "Approved for Payment - Bankline"
    APPROVED_FOREIGN_POA = "Approved for Payment - Foreign POA"
    # Older records only; current code routes foreign payments to
    # APPROVED_FOREIGN_POA.
    FOREIGN_AWAITING_ALLOCATION = "Foreign Payment / Awaiting Allocation"

    PAID = "Paid / Awaiting Bank Reconciliation"
    RECONCILED = "Reconciled / Complete"

    REJECTED = "Rejected"
    CANCELLED_DUPLICATE = "Cancelled - Duplicate"
    STATEMENT_FILED = "Statement Filed"


def awaiting_approval(level: int) -> str:
    return Status.AWAITING_APPROVAL_1 if level == 1 else Status.AWAITING_APPROVAL_2


def processing_approval(level: int) -> str:
    """Held while an approval decision is being saved (like
    PROCESSING_CONFIRMATION)."""
    return f"Processing Approval {level}"


TERMINAL_STATUSES = (
    Status.RECONCILED,
    Status.STATEMENT_FILED,
    Status.REJECTED,
    Status.CANCELLED_DUPLICATE,
)
