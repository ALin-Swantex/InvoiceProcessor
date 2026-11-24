"""Stage emails (sent at most once per stage) and the scheduled approval reminders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

from app.activity_feed import ROLE_APPROVER_1, ROLE_APPROVER_2
from app.email_notifications import send_email_notification
from app.invoices import InvoiceRecord
from app.statuses import Status
from app.lifecycle.base import LifecycleBase


class NotificationStage(LifecycleBase):
    def _send_stage_email_once(
        self,
        invoice_id: int,
        stage: str,
        *,
        recipient: str,
        subject: str,
        body: str,
    ) -> None:
        """Send at most one email for a stage across retries and query loops."""
        if not self.activity_feed.claim_email_stage(invoice_id, stage):
            return
        try:
            sent = send_email_notification(
                recipient=recipient,
                subject=subject,
                body=body,
            )
        except Exception:
            self.activity_feed.release_email_stage(invoice_id, stage)
            raise
        if sent is False:
            self.activity_feed.release_email_stage(invoice_id, stage)
            return
        self.activity_feed.mark_email_stage_sent(invoice_id, stage)
        self._log_purchase_ledger(
            event_type="stage_email_sent",
            message=f"Stage email '{stage}' sent to {recipient}.",
            invoice_id=invoice_id,
        )

    def _send_stage_email(
        self,
        invoice_id: int,
        stage: str,
        *,
        recipient: str,
        subject: str,
        body: str,
    ) -> None:
        """Send a workflow stage email without failing the step it follows.

        The step's new status is already saved when this runs, so raising
        would show the user an error for work that succeeded and skip what
        comes after it. A failed send is logged for Purchase Ledger instead."""
        try:
            self._send_stage_email_once(
                invoice_id, stage, recipient=recipient, subject=subject, body=body
            )
        except Exception as error:
            self._log_purchase_ledger(
                event_type="email_notification_failed",
                message=f"The '{stage}' email to {recipient} failed: {error}",
                invoice_id=invoice_id,
            )

    @staticmethod
    def _app_url(tab: str) -> str:
        base = os.environ.get("APP_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
        return f"{base}/?tab={tab}"

    def _approval_email_body(self, invoice: InvoiceRecord, level: int, *, reminder: bool) -> str:
        prefix = "Reminder: " if reminder else ""
        return (
            f"{prefix}invoice {invoice.irj_number or invoice.original_filename} "
            f"({invoice.supplier or 'supplier unknown'}) is waiting for your approval.\n\n"
            f"View all pending invoices: {self._app_url(f'approver{level}')}"
        )

    def _notify_purchase_ledger_rejection(self, invoice: InvoiceRecord) -> None:
        try:
            self._send_stage_email_once(
                invoice.id,
                "rejected_purchase_ledger",
                recipient=os.environ.get(
                    "PURCHASE_LEDGER_NOTIFICATION_EMAIL",
                    "purchase-ledger@example.test",
                ),
                subject=f"Invoice ({invoice.original_filename}) rejected",
                body=(
                    f"Invoice {invoice.irj_number or invoice.original_filename} "
                    f"({invoice.supplier or 'supplier unknown'}) was rejected.\n"
                    f"Reason: {invoice.rejection_reason or 'No reason recorded.'}\n\n"
                    f"View rejected invoices: {self._app_url('rejected')}"
                ),
            )
        except Exception as error:
            self._log_purchase_ledger(
                event_type="email_notification_failed",
                message=f"Purchase Ledger rejection email failed: {error}",
                invoice_id=invoice.id,
            )

    def process_scheduled_notifications(self, *, now: datetime | None = None) -> int:
        """Send due recurring approval reminders and retry rejection notices."""
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        sent = 0
        for invoice in self.invoice_store.list_all_with_status(
            [Status.REJECTED, Status.AWAITING_APPROVAL_1, Status.AWAITING_APPROVAL_2]
        ):
            if invoice.status == Status.REJECTED:
                # Retry only recent rejections, so enabling emails does not
                # send one for every rejection ever recorded.
                rejected_at = self._as_utc(
                    invoice.approver2_date
                    if invoice.approver2_decision == "rejected"
                    else invoice.approver1_date
                    if invoice.approver1_decision == "rejected"
                    else invoice.reviewed_at or invoice.created_at
                )
                if rejected_at is not None and current - rejected_at <= timedelta(
                    days=7
                ):
                    self._notify_purchase_ledger_rejection(invoice)
                continue
            level = 1 if invoice.status.endswith("1") else 2
            recipient = getattr(invoice, f"approver{level}_email")
            if not recipient:
                continue
            on_hold = invoice.hold_level == level
            if on_hold:
                anchor = self._as_utc(
                    invoice.approval_hold_reminder_sent_at
                    or invoice.approval_hold_started_at
                )
                interval = timedelta(days=30)
                timestamp_field = "approval_hold_reminder_sent_at"
                stage_prefix = "approval_hold_reminder"
            else:
                anchor = self._as_utc(
                    invoice.approval_reminder_sent_at or invoice.approval_requested_at
                )
                interval = timedelta(days=7)
                timestamp_field = "approval_reminder_sent_at"
                stage_prefix = "approval_reminder"
            if anchor is None or current < anchor + interval:
                continue
            due_at = anchor + interval
            stage = f"{stage_prefix}_{level}_{due_at.date().isoformat()}"
            before = self.activity_feed.claim_email_stage(invoice.id, stage)
            if not before:
                continue
            try:
                did_send = send_email_notification(
                    recipient=str(recipient),
                    subject=(
                        f"{'On-hold ' if on_hold else ''}invoice approval reminder: "
                        f"{invoice.irj_number or invoice.original_filename}"
                    ),
                    body=self._approval_email_body(invoice, level, reminder=True),
                )
            except Exception as error:
                self.activity_feed.release_email_stage(invoice.id, stage)
                self.activity_feed.add_event(
                    event_type="email_notification_failed",
                    target_role=(
                        ROLE_APPROVER_1 if level == 1 else ROLE_APPROVER_2
                    ),
                    message=f"Approval reminder email failed: {error}",
                    invoice_id=invoice.id,
                )
                continue
            if not did_send:
                self.activity_feed.release_email_stage(invoice.id, stage)
                continue
            self.activity_feed.mark_email_stage_sent(invoice.id, stage)
            self.invoice_store.update_fields(
                invoice.id, **{timestamp_field: current.isoformat()}
            )
            self.activity_feed.add_event(
                event_type="approval_reminder_sent",
                target_role=ROLE_APPROVER_1 if level == 1 else ROLE_APPROVER_2,
                message=(
                    f"{'30-day on-hold' if on_hold else '7-day'} approval reminder "
                    f"sent to {recipient}."
                ),
                invoice_id=invoice.id,
            )
            sent += 1
        return sent
