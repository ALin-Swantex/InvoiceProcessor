"""Stage 3: Approver 1 / Approver 2 decisions and resuming approval queries."""

from __future__ import annotations

from datetime import datetime, timezone
import os

from app.activity_feed import ROLE_APPROVER_1, ROLE_APPROVER_2
from app.company_folders import REJECTED_INVOICES_FOLDER
from app.invoices import InvoiceRecord
from app.statuses import Status, awaiting_approval, processing_approval
from app.lifecycle.common import InvoiceLifecycleError, _append_history_entry
from app.lifecycle.base import LifecycleBase


class ApprovalStage(LifecycleBase):
    def decide_approval(
        self,
        invoice_id: int,
        *,
        level: int,
        decision: str,
        comments: str | None,
        recorded_by: str,
    ) -> InvoiceRecord:
        if level not in (1, 2):
            raise InvoiceLifecycleError("Approval level must be 1 or 2.")
        if decision not in ("approved", "rejected", "on_hold"):
            raise InvoiceLifecycleError(
                "Decision must be 'approved', 'rejected', or 'on_hold'."
            )

        invoice = self._require_invoice(invoice_id)
        expected_status = awaiting_approval(level)
        if invoice.status != expected_status:
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is not {expected_status} (status: {invoice.status})."
            )
        if decision == "on_hold" and not comments:
            raise InvoiceLifecycleError(
                "A comment explaining the hold is required."
            )

        now = datetime.now(timezone.utc).isoformat()
        with self._claimed(
            invoice,
            processing_approval(level),
            refusal=f"Invoice {invoice_id} is not {expected_status}",
        ) as commit_decision:
            if decision == "on_hold":
                # A query is metadata on the current approval stage, not a routing
                # decision. Do not move the PDF or change its visible stage.
                history = _append_history_entry(
                    invoice.hold_reason,
                    f"{recorded_by} recorded approval query",
                    comments,
                )
                fields = {
                    "status": expected_status,
                    "hold_level": level,
                    "hold_reason": history,
                    "approval_hold_started_at": (
                        invoice.approval_hold_started_at or now
                    ),
                    "approval_hold_reminder_sent_at": None,
                    f"approver{level}_comments": getattr(
                        invoice, f"approver{level}_comments"
                    ),
                }
                record = commit_decision(**fields)
                self._log_purchase_ledger(
                    event_type="approval_on_hold",
                    message=(
                        f"Invoice {invoice.irj_number}: {recorded_by} placed it "
                        f"on hold — {comments}"
                    ),
                    invoice_id=invoice_id,
                )
                return record

            if decision == "rejected":
                fields = {
                    f"approver{level}_decision": "rejected",
                    f"approver{level}_date": now,
                    f"approver{level}_comments": (
                        _append_history_entry(
                            getattr(invoice, f"approver{level}_comments"),
                            f"{recorded_by} rejected",
                            comments,
                        )
                        if comments
                        else getattr(invoice, f"approver{level}_comments")
                    ),
                    "status": Status.REJECTED,
                    "hold_level": None,
                    "rejection_reason": comments,
                    "approval_requested_at": None,
                    "approval_reminder_sent_at": None,
                    "approval_hold_started_at": None,
                    "approval_hold_reminder_sent_at": None,
                }
                self._move_pdf_in_sharepoint(
                    invoice, REJECTED_INVOICES_FOLDER, self._rejected_filename(invoice)
                )
                record = commit_decision(**fields)
                self._log_purchase_ledger(
                    event_type="rejected",
                    message=f"Invoice {invoice.irj_number} rejected by {recorded_by}.",
                    invoice_id=invoice_id,
                )
                self._notify_purchase_ledger_rejection(record)
                return record

            if level == 1 and invoice.approver2_name:
                fields = {
                    "approver1_decision": "approved",
                    "approver1_date": now,
                    "approver1_comments": (
                        _append_history_entry(
                            invoice.approver1_comments,
                            f"{recorded_by} approved",
                            comments,
                        )
                        if comments
                        else invoice.approver1_comments
                    ),
                    "status": Status.AWAITING_APPROVAL_2,
                    "hold_level": None,
                    "approval_requested_at": now,
                    "approval_reminder_sent_at": None,
                    "approval_hold_started_at": None,
                    "approval_hold_reminder_sent_at": None,
                }
                self._move_pdf_in_sharepoint(
                    invoice,
                    self._company_folders(invoice).nominal_approver_2,
                    self._filed_filename(invoice),
                )
                record = commit_decision(**fields)
                self._send_stage_email(
                    invoice_id,
                    "approval_2",
                    recipient=str(invoice.approver2_email),
                    subject=(
                        f"New invoice ({invoice.original_filename}) waiting for "
                        "approval"
                    ),
                    body=self._approval_email_body(record, 2, reminder=False),
                )
                self.activity_feed.add_event(
                    event_type="approval_pending",
                    target_role=ROLE_APPROVER_2,
                    message=(
                        f"Invoice {invoice.irj_number} ({invoice.supplier}) is "
                        "awaiting Approver 2 approval."
                    ),
                    invoice_id=invoice_id,
                )
                return record

            fields = {
                f"approver{level}_decision": "approved",
                f"approver{level}_date": now,
                f"approver{level}_comments": (
                    _append_history_entry(
                        getattr(invoice, f"approver{level}_comments"),
                        f"{recorded_by} approved",
                        comments,
                    )
                    if comments
                    else getattr(invoice, f"approver{level}_comments")
                ),
                "status": Status.APPROVED,
                "hold_level": None,
                "approval_requested_at": None,
                "approval_reminder_sent_at": None,
                "approval_hold_started_at": None,
                "approval_hold_reminder_sent_at": None,
            }
            self._move_pdf_in_sharepoint(
                invoice,
                self._company_folders(invoice).approved_for_payment,
                self._filed_filename(invoice),
            )
            record = commit_decision(**fields)
            # Once fully approved, the Purchase Ledger team should receive an
            # email notification where appropriate. Purchase Ledger isn't the one clicking approve here
            # (an approver is), so -- unlike the PO-matched branch above, where
            # Purchase Ledger performed the action themselves -- they need an
            # actual email, not just an activity feed entry they'd have to go
            # looking for.
            self._send_stage_email(
                invoice_id,
                "approved_for_payment",
                recipient=os.environ.get(
                    "PURCHASE_LEDGER_NOTIFICATION_EMAIL",
                    "purchase-ledger@example.test",
                ),
                subject=f"Invoice ({invoice.original_filename}) fully approved",
                body=(
                    f"Invoice {invoice.irj_number} ({invoice.supplier}) has completed "
                    "all required nominal approvals and is ready for payment."
                ),
            )
            self._log_purchase_ledger(
                event_type="approved",
                message=f"Invoice {invoice.irj_number} fully approved and ready for payment.",
                invoice_id=invoice_id,
            )
            return self.auto_route_for_payment(record.id)

    def resume_approval(
        self,
        invoice_id: int,
        *,
        resolution_notes: str | None,
        recorded_by: str,
    ) -> InvoiceRecord:
        """Clear an approval query without advancing the invoice."""
        invoice = self._require_invoice(invoice_id)
        if invoice.status == Status.WORKFLOW_ISSUE and invoice.review_return_status:
            return_status = invoice.review_return_status
            record = self._transition(
                invoice,
                return_status,
                review_return_status=None,
                hold_reason=_append_history_entry(
                    invoice.hold_reason,
                    f"{recorded_by} resolved workflow issue",
                    resolution_notes or "Resolved without additional notes.",
                ),
            )
            self._log_purchase_ledger(
                event_type="workflow_resumed",
                message=(
                    f"Invoice {invoice.irj_number or invoice.original_filename} issue "
                    f"resolved; returned to {return_status}."
                ),
                invoice_id=invoice_id,
            )
            return record
        legacy_hold = invoice.status == "Approval Query / On Hold"
        level = invoice.hold_level or (1 if legacy_hold else None)
        expected_status = awaiting_approval(level) if level in (1, 2) else None
        if expected_status is None or (
            not legacy_hold and invoice.status != expected_status
        ):
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is not on hold (status: {invoice.status})."
            )
        # A current hold never moved the PDF; only a legacy hold status did.
        record = self._transition(
            invoice,
            expected_status,
            move_pdf=legacy_hold,
            hold_level=None,
            hold_reason=_append_history_entry(
                invoice.hold_reason,
                f"{recorded_by} resolved approval query",
                resolution_notes or "Resolved without additional notes.",
            ),
            approval_requested_at=datetime.now(timezone.utc).isoformat(),
            approval_reminder_sent_at=None,
            approval_hold_started_at=None,
            approval_hold_reminder_sent_at=None,
        )
        self.activity_feed.add_event(
            event_type="approval_resumed",
            target_role=ROLE_APPROVER_1 if level == 1 else ROLE_APPROVER_2,
            message=(
                f"Invoice {invoice.irj_number}: query resolved by {recorded_by}; "
                f"awaiting {invoice.approver1_name if level == 1 else invoice.approver2_name} again."
            ),
            invoice_id=invoice_id,
        )
        return record
