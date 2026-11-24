"""Errors, constants and small helpers shared by the lifecycle stages."""

from __future__ import annotations

from datetime import datetime
import sqlite3
from zoneinfo import ZoneInfo

from app.statuses import Status


class InvoiceLifecycleError(ValueError):
    """Raised for business-rule violations (invalid state transitions,
    unknown company, etc.). Callers should map this to HTTP 422/409."""


def _is_unique_violation(error: BaseException) -> bool:
    """True for a unique-constraint failure from SQLite or PostgreSQL (psycopg)."""
    if isinstance(error, sqlite3.IntegrityError):
        return True
    return type(error).__name__ == "UniqueViolation"


AUTO_CONFIRM_ACTOR = "AI auto-confirm"

# The company SharePoint folder (a CompanyFolderStructure attribute) for each
# status. Awaiting AI Extraction (the Incoming folder) and Awaiting Sage
# Registration (depends on the invoice type) are handled in _folder_for_status;
# statuses not listed here have no folder to move to.
STATUS_FOLDERS = {
    Status.AWAITING_PO_MATCHING: "po_match",
    Status.PO_QUERY: "po_match",
    Status.AWAITING_APPROVAL_1: "nominal_approver_1",
    Status.AWAITING_APPROVAL_2: "nominal_approver_2",
    Status.WORKFLOW_ISSUE: "nominal_on_hold",
    Status.APPROVED: "approved_for_payment",
    Status.PAYMENT_ROUTING_ISSUE: "approved_for_payment",
    Status.APPROVED_BACS: "approved_bacs",
    Status.APPROVED_BANKLINE: "approved_bankline",
    Status.APPROVED_FOREIGN_POA: "approved_foreign_poa",
    Status.PAID: "paid",
    Status.RECONCILED: "reconciled",
}


class InvoiceExtractionUnavailableError(InvoiceLifecycleError):
    pass


def _append_history_entry(
    existing: str | None,
    label: str,
    detail: str,
) -> str:
    timestamp = datetime.now(ZoneInfo("Europe/London")).strftime(
        "%d/%m/%Y %H:%M %Z"
    )
    entry = f"[{timestamp}] {label}: {detail.strip()}"
    return f"{existing.rstrip()}\n\n{entry}" if existing else entry
