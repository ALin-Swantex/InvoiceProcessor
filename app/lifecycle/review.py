"""Manual review: flagging, flagged-review decisions, statements and duplicate deletion."""

from __future__ import annotations

from pathlib import Path

from app.company_folders import REJECTED_INVOICES_FOLDER, statement_company_folder
from app.invoices import InvoiceRecord
from app.sharepoint import SharePointError
from app.statuses import TERMINAL_STATUSES, Status
from app.lifecycle.common import InvoiceLifecycleError, _append_history_entry
from app.lifecycle.base import LifecycleBase


class ReviewStage(LifecycleBase):
    def file_statement(
        self,
        invoice_id: int,
        *,
        company: str,
        recorded_by: str,
    ) -> InvoiceRecord:
        document = self._require_invoice(invoice_id)
        if document.status != Status.NEEDS_REVIEW:
            raise InvoiceLifecycleError(
                "Only a flagged document can be filed as a supplier statement."
            )
        company_profile = self.companies_store.get(company)
        if company_profile is None:
            raise InvoiceLifecycleError(
                f"'{company}' is not a recognised company."
            )
        if self.sharepoint_client is None:
            raise InvoiceLifecycleError(
                "SharePoint must be configured before a statement can be filed."
            )
        destination = statement_company_folder(company_profile.name)
        record = self._transition(
            document,
            Status.STATEMENT_FILED,
            folder=destination,
            filename=document.original_filename,
            document_type="statement",
            company=company_profile.name,
            review_reason=None,
            review_return_status=None,
        )
        self._log_purchase_ledger(
            event_type="statement_filed",
            message=(
                f"Statement '{document.original_filename}' filed to "
                f"{destination} by {recorded_by}."
            ),
            invoice_id=invoice_id,
        )
        return record

    def mark_as_invoice(
        self, invoice_id: int, *, recorded_by: str
    ) -> InvoiceRecord:
        document = self._require_invoice(invoice_id)
        if (
            document.status != Status.NEEDS_REVIEW
            or document.document_type != "statement"
        ):
            raise InvoiceLifecycleError(
                "Only a flagged supplier statement can be reclassified as an invoice."
            )
        record = self._transition(
            document,
            Status.NEEDS_REVIEW,
            move_pdf=False,
            document_type="invoice",
            review_reason="Awaiting Purchase Ledger invoice confirmation.",
            ai_review_warnings=None,
            review_return_status=None,
        )
        self._log_purchase_ledger(
            event_type="document_reclassified",
            message=(
                f"'{document.original_filename}' was marked as an invoice "
                f"by {recorded_by}."
            ),
            invoice_id=invoice_id,
        )
        return record

    def flag_for_review(self, invoice_id: int, *, reason: str) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status == Status.NEEDS_REVIEW:
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is already awaiting review."
            )
        initial_stage = invoice.status == Status.AWAITING_EXTRACTION
        # Only an active stage can be held: the hold records it as the stage
        # to return to, and its PDF needs a folder to go back to.
        if not initial_stage and (
            invoice.status in TERMINAL_STATUSES
            or invoice.status == Status.WORKFLOW_ISSUE
            or invoice.status.startswith("Processing ")
            or self._folder_for_status(invoice, invoice.status) is None
        ):
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} cannot be flagged for review from status "
                f"{invoice.status}."
            )
        if not reason.strip():
            raise InvoiceLifecycleError("A review reason is required.")
        if initial_stage:
            with self._claimed(invoice) as save:
                self._move_to_flagged(invoice)
                record = save(
                    status=Status.NEEDS_REVIEW,
                    review_reason=reason.strip(),
                    review_return_status=invoice.status,
                )
            self._log_purchase_ledger(
                event_type="needs_review",
                message=f"Invoice flagged for review: {reason}",
                invoice_id=invoice_id,
            )
            return record
        record = self._transition(
            invoice,
            Status.WORKFLOW_ISSUE,
            hold_reason=_append_history_entry(
                invoice.hold_reason, "Workflow issue recorded", reason
            ),
            review_return_status=invoice.status,
        )
        self._log_purchase_ledger(
            event_type="needs_review",
            message=f"Invoice flagged for review: {reason}",
            invoice_id=invoice_id,
        )
        return record

    def resolve_flagged_review(
        self,
        invoice_id: int,
        *,
        accepted: bool,
        reason: str | None,
        recorded_by: str,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status != Status.NEEDS_REVIEW or not invoice.review_return_status:
            raise InvoiceLifecycleError(
                "This invoice requires its dedicated review action and cannot "
                "be resolved as a manually flagged invoice."
            )
        return_status = invoice.review_return_status
        if accepted:
            record = self._transition(
                invoice,
                return_status,
                review_reason=None,
                review_return_status=None,
            )
            self._log_purchase_ledger(
                event_type="review_accepted",
                message=(
                    f"Invoice {invoice.irj_number or invoice.original_filename} "
                    f"accepted by {recorded_by} and returned to {return_status}."
                ),
                invoice_id=invoice_id,
            )
            return record

        if not reason or not reason.strip():
            raise InvoiceLifecycleError("A rejection reason is required.")
        record = self._transition(
            invoice,
            Status.REJECTED,
            folder=REJECTED_INVOICES_FOLDER,
            filename=self._rejected_filename(invoice),
            rejection_reason=reason.strip(),
            review_reason=None,
            review_return_status=None,
        )
        self._log_purchase_ledger(
            event_type="rejected",
            message=(
                f"Invoice {invoice.irj_number or invoice.original_filename} "
                f"rejected during review by {recorded_by}: {reason.strip()}"
            ),
            invoice_id=invoice_id,
        )
        self._notify_purchase_ledger_rejection(record)
        return record

    def reject_flagged_invoice(
        self, invoice_id: int, *, reason: str, recorded_by: str
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status != Status.NEEDS_REVIEW or invoice.document_type != "invoice":
            raise InvoiceLifecycleError(
                "Only a flagged invoice can be rejected from this section."
            )
        if not reason.strip():
            raise InvoiceLifecycleError("A rejection reason is required.")
        record = self._transition(
            invoice,
            Status.REJECTED,
            folder=REJECTED_INVOICES_FOLDER,
            filename=self._rejected_filename(invoice),
            rejection_reason=reason.strip(),
            review_reason=None,
            review_return_status=None,
        )
        self._log_purchase_ledger(
            event_type="rejected",
            message=(
                f"Flagged invoice {invoice.irj_number or invoice.original_filename} "
                f"rejected by {recorded_by}: {reason.strip()}"
            ),
            invoice_id=invoice_id,
        )
        self._notify_purchase_ledger_rejection(record)
        return record

    def delete_invoice(self, invoice_id: int, *, recorded_by: str) -> None:
        invoice = self._require_invoice(invoice_id)
        if invoice.status != Status.NEEDS_REVIEW or invoice.duplicate_of_invoice_id is None:
            raise InvoiceLifecycleError(
                "Only a possible duplicate in Incoming review can be permanently deleted."
            )

        if invoice.sharepoint_item_id:
            if self.sharepoint_client is None:
                raise InvoiceLifecycleError(
                    "The SharePoint PDF cannot be deleted because SharePoint "
                    "is not configured."
                )
            try:
                self.sharepoint_client.delete_item(invoice.sharepoint_item_id)
            except SharePointError as error:
                raise InvoiceLifecycleError(str(error)) from error

        pdf_path = Path(invoice.stored_path)
        try:
            if pdf_path.is_file() or pdf_path.is_symlink():
                pdf_path.unlink()
        except OSError as error:
            raise InvoiceLifecycleError(
                f"The local invoice PDF could not be deleted: {error}"
            ) from error

        try:
            self.invoice_store.delete(invoice_id)
        except KeyError as error:
            raise InvoiceLifecycleError(str(error)) from error
        # No invoice_id: the row is gone, and PostgreSQL's foreign key from
        # activity events to invoices would reject the event.
        self._log_purchase_ledger(
            event_type="invoice_deleted",
            message=(
                f"'{invoice.original_filename}' (invoice #{invoice_id}) was "
                f"permanently deleted by {recorded_by} before approval/payment "
                "processing."
            ),
        )
