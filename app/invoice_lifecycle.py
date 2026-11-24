"""The invoice workflow: every stage from AI extraction to bank reconciliation.

Each stage lives in its own module under app/lifecycle/; this module puts them
together as InvoiceLifecycle and keeps the names callers import from here.
"""

from __future__ import annotations

from app.lifecycle.approval import ApprovalStage
from app.lifecycle.common import (
    AUTO_CONFIRM_ACTOR,
    STATUS_FOLDERS,
    InvoiceExtractionUnavailableError,
    InvoiceLifecycleError,
    _is_unique_violation,
)
from app.lifecycle.confirmation import ConfirmationStage
from app.lifecycle.extraction import ExtractionStage
from app.lifecycle.notifications import NotificationStage
from app.lifecycle.payment import PaymentStage
from app.lifecycle.review import ReviewStage

__all__ = [
    "AUTO_CONFIRM_ACTOR",
    "STATUS_FOLDERS",
    "InvoiceExtractionUnavailableError",
    "InvoiceLifecycle",
    "InvoiceLifecycleError",
    "_is_unique_violation",
]


class InvoiceLifecycle(
    ExtractionStage,
    ConfirmationStage,
    ApprovalStage,
    ReviewStage,
    PaymentStage,
    NotificationStage,
):
    """The whole invoice workflow. See app/lifecycle/base.py for how a stage
    moves an invoice between statuses."""
