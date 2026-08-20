from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.activity_feed import (
    ROLE_APPROVER_1,
    ROLE_APPROVER_2,
    ROLE_PURCHASE_LEDGER,
    ROLE_PURCHASING,
    ActivityFeedStore,
)
from app.ai_extraction import run_ai_extraction
from app.approval_matrix import find_approvers
from app.companies import get_company
from app.email_notifications import send_email_notification
from app.invoices import InvoiceRecord, InvoiceStore
from app.irj import IrjNumberGenerator
from app.sharepoint import SharePointClient, SharePointError
from app.workflow import ConfirmedInvoice, RoutingValidationError, route_confirmed_invoice


class InvoiceLifecycleError(ValueError):
    """Raised for business-rule violations (invalid state transitions,
    unknown company, etc.). Callers should map this to HTTP 422/409."""


class InvoiceLifecycle:
    """Orchestrates every stage of the invoice workflow described in
    SOFTWARE_SPEC.md: AI extraction, company/IRJ/routing, SharePoint filing,
    PO matching, nominal approvals (Approver 1 / Approver 2), payment, and
    bank reconciliation.

    `sharepoint_client` may be None (e.g. no SharePoint site configured yet).
    In that case the workflow still progresses through every status; the
    SharePoint move is simply skipped and recorded as a pending integration
    step in the activity feed, so the rest of the process can be tested
    before SharePoint credentials exist.
    """

    def __init__(
        self,
        invoice_store: InvoiceStore,
        irj_generator: IrjNumberGenerator,
        activity_feed: ActivityFeedStore,
        sharepoint_client: SharePointClient | None,
    ) -> None:
        self.invoice_store = invoice_store
        self.irj_generator = irj_generator
        self.activity_feed = activity_feed
        self.sharepoint_client = sharepoint_client

    # ------------------------------------------------------------------
    # Stage 1: AI extraction (placeholder)
    # ------------------------------------------------------------------

    def run_extraction(self, invoice_id: int) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        result = run_ai_extraction(Path(invoice.stored_path))
        if result.needs_review:
            record = self.invoice_store.update_fields(
                invoice_id,
                status="Needs Review",
                review_reason=(
                    "AI extraction is not yet connected (placeholder). "
                    "Purchase Ledger must confirm invoice details manually."
                ),
            )
            self.activity_feed.add_event(
                event_type="needs_review",
                target_role=ROLE_PURCHASE_LEDGER,
                message=(
                    f"Invoice {invoice.original_filename} needs manual "
                    "confirmation before filing."
                ),
                invoice_id=invoice_id,
            )
            return record
        # Reached only once real AI extraction is connected and confident.
        return self.invoice_store.update_fields(
            invoice_id,
            company=result.company,
            supplier=result.supplier,
            supplier_invoice_number=result.supplier_invoice_number,
            po_number=result.purchase_order_number,
            invoice_date=result.invoice_date,
            invoice_value=result.invoice_value,
            currency=result.currency,
            status="Needs Review",
            review_reason="Awaiting Purchase Ledger confirmation.",
        )

    # ------------------------------------------------------------------
    # Stage 2: Purchase Ledger confirmation, IRJ numbering, routing, filing
    # ------------------------------------------------------------------

    def confirm_and_route(
        self,
        invoice_id: int,
        *,
        company: str,
        supplier: str,
        supplier_invoice_number: str | None,
        purchase_order_number: str | None,
        invoice_date: str | None,
        invoice_value: float | None,
        currency: str | None,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        company_profile = get_company(company)
        if company_profile is None:
            raise InvoiceLifecycleError(
                f"'{company}' is not a recognised company. Add it to app/companies.py "
                "(or the production Companies SharePoint List) first."
            )

        irj_number = self.irj_generator.generate()
        try:
            decision = route_confirmed_invoice(
                ConfirmedInvoice(
                    invoice_id=str(invoice_id),
                    company=company,
                    company_folder=company_profile.company_folder,
                    original_filename=invoice.original_filename,
                    irj_number=irj_number,
                    purchase_order_number=purchase_order_number,
                    po_matching_folder=company_profile.po_matching_folder,
                    purchase_ledger_recipient="purchase-ledger@example.test",
                )
            )
        except RoutingValidationError as error:
            raise InvoiceLifecycleError(str(error)) from error

        self._move_pdf_in_sharepoint(invoice, decision.destination_folder, decision.destination_filename)

        base_fields: dict[str, object] = {
            "company": company,
            "supplier": supplier,
            "supplier_invoice_number": supplier_invoice_number,
            "po_number": purchase_order_number or None,
            "invoice_date": invoice_date,
            "invoice_value": invoice_value,
            "currency": currency,
            "irj_number": irj_number,
        }

        if decision.route == "purchase_order":
            record = self.invoice_store.update_fields(
                invoice_id,
                **base_fields,
                invoice_type="po",
                status=decision.status,
                review_reason=None,
            )
            self.activity_feed.add_event(
                event_type="po_awaiting_match",
                target_role=ROLE_PURCHASE_LEDGER,
                message=(
                    f"Invoice {irj_number} ({supplier}) is awaiting PO matching "
                    f"against PO {purchase_order_number}."
                ),
                invoice_id=invoice_id,
            )
            return record

        entry = find_approvers(company, supplier)
        if entry is None:
            record = self.invoice_store.update_fields(
                invoice_id,
                **base_fields,
                invoice_type="nominal",
                status="Needs Review",
                review_reason=(
                    f"No approval matrix entry found for supplier '{supplier}' "
                    f"under '{company}'."
                ),
            )
            self.activity_feed.add_event(
                event_type="needs_review",
                target_role=ROLE_PURCHASE_LEDGER,
                message=(
                    f"Invoice {irj_number}: no approver configured for "
                    f"'{supplier}' under '{company}'."
                ),
                invoice_id=invoice_id,
            )
            return record

        record = self.invoice_store.update_fields(
            invoice_id,
            **base_fields,
            invoice_type="nominal",
            status="Awaiting Approval 1",
            approver1_name=entry.approver1.name,
            approver1_email=entry.approver1.email,
            approver2_name=entry.approver2.name if entry.approver2 else None,
            approver2_email=entry.approver2.email if entry.approver2 else None,
            review_reason=None,
        )
        send_email_notification(
            recipient=entry.approver1.email,
            subject=f"Invoice {irj_number} awaiting your approval",
            body=f"Invoice {irj_number} from {supplier} requires your approval.",
        )
        self.activity_feed.add_event(
            event_type="approval_pending",
            target_role=ROLE_APPROVER_1,
            message=f"Invoice {irj_number} ({supplier}) is awaiting Approver 1 approval.",
            invoice_id=invoice_id,
        )
        return record

    # ------------------------------------------------------------------
    # Stage 3a: PO matching
    # ------------------------------------------------------------------

    def record_po_match(
        self, invoice_id: int, *, matched: bool, notes: str | None
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status not in ("Awaiting PO Matching", "PO Query / Matching Issue"):
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is not awaiting PO matching (status: {invoice.status})."
            )
        if matched:
            record = self.invoice_store.update_fields(
                invoice_id, status="Approved", po_query_notes=notes
            )
            self.activity_feed.add_event(
                event_type="approved",
                target_role=ROLE_PURCHASE_LEDGER,
                message=f"Invoice {invoice.irj_number} matched against PO and approved.",
                invoice_id=invoice_id,
            )
            return record

        record = self.invoice_store.update_fields(
            invoice_id, status="PO Query / Matching Issue", po_query_notes=notes
        )
        self.activity_feed.add_event(
            event_type="po_query",
            target_role=ROLE_PURCHASING,
            message=f"Invoice {invoice.irj_number}: PO matching issue — {notes or 'see notes'}.",
            invoice_id=invoice_id,
        )
        return record

    # ------------------------------------------------------------------
    # Stage 3b: Nominal approvals (Approver 1 / Approver 2)
    # ------------------------------------------------------------------

    def decide_approval(
        self,
        invoice_id: int,
        *,
        level: int,
        decision: str,
        comments: str | None,
    ) -> InvoiceRecord:
        if level not in (1, 2):
            raise InvoiceLifecycleError("Approval level must be 1 or 2.")
        if decision not in ("approved", "rejected"):
            raise InvoiceLifecycleError("Decision must be 'approved' or 'rejected'.")

        invoice = self._require_invoice(invoice_id)
        expected_status = f"Awaiting Approval {level}"
        if invoice.status != expected_status:
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is not {expected_status} (status: {invoice.status})."
            )

        now = datetime.now(timezone.utc).isoformat()

        if decision == "rejected":
            fields = {
                f"approver{level}_decision": "rejected",
                f"approver{level}_date": now,
                f"approver{level}_comments": comments,
                "status": "Rejected",
                "rejection_reason": comments,
            }
            record = self.invoice_store.update_fields(invoice_id, **fields)
            self.activity_feed.add_event(
                event_type="rejected",
                target_role=ROLE_PURCHASE_LEDGER,
                message=f"Invoice {invoice.irj_number} rejected by Approver {level}.",
                invoice_id=invoice_id,
            )
            return record

        if level == 1 and invoice.approver2_email:
            fields = {
                "approver1_decision": "approved",
                "approver1_date": now,
                "approver1_comments": comments,
                "status": "Awaiting Approval 2",
            }
            record = self.invoice_store.update_fields(invoice_id, **fields)
            send_email_notification(
                recipient=str(invoice.approver2_email),
                subject=f"Invoice {invoice.irj_number} awaiting your approval",
                body=f"Invoice {invoice.irj_number} requires your approval.",
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
            f"approver{level}_comments": comments,
            "status": "Approved",
        }
        record = self.invoice_store.update_fields(invoice_id, **fields)
        self.activity_feed.add_event(
            event_type="approved",
            target_role=ROLE_PURCHASE_LEDGER,
            message=f"Invoice {invoice.irj_number} fully approved and ready for payment.",
            invoice_id=invoice_id,
        )
        return record

    # ------------------------------------------------------------------
    # Manual review flag
    # ------------------------------------------------------------------

    def flag_for_review(self, invoice_id: int, *, reason: str) -> InvoiceRecord:
        self._require_invoice(invoice_id)
        record = self.invoice_store.update_fields(
            invoice_id, status="Needs Review", review_reason=reason
        )
        self.activity_feed.add_event(
            event_type="needs_review",
            target_role=ROLE_PURCHASE_LEDGER,
            message=f"Invoice flagged for review: {reason}",
            invoice_id=invoice_id,
        )
        return record

    # ------------------------------------------------------------------
    # Stage 4: Payment and bank reconciliation
    # ------------------------------------------------------------------

    def mark_paid(
        self, invoice_id: int, *, payment_date: str, payment_reference: str | None
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status != "Approved":
            raise InvoiceLifecycleError(
                f"Only Approved invoices can be marked as paid (status: {invoice.status})."
            )
        record = self.invoice_store.update_fields(
            invoice_id,
            status="Paid / Awaiting Bank Reconciliation",
            payment_date=payment_date,
            payment_reference=payment_reference,
        )
        self.activity_feed.add_event(
            event_type="paid",
            target_role=ROLE_PURCHASE_LEDGER,
            message=f"Invoice {invoice.irj_number} paid; awaiting bank reconciliation.",
            invoice_id=invoice_id,
        )
        return record

    def mark_reconciled(
        self, invoice_id: int, *, reconciliation_date: str, notes: str | None
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status != "Paid / Awaiting Bank Reconciliation":
            raise InvoiceLifecycleError(
                f"Only invoices awaiting bank reconciliation can be reconciled "
                f"(status: {invoice.status})."
            )
        record = self.invoice_store.update_fields(
            invoice_id,
            status="Reconciled / Complete",
            reconciliation_date=reconciliation_date,
            reconciliation_notes=notes,
        )
        self.activity_feed.add_event(
            event_type="reconciled",
            target_role=ROLE_PURCHASE_LEDGER,
            message=f"Invoice {invoice.irj_number} reconciled and filed.",
            invoice_id=invoice_id,
        )
        return record

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_invoice(self, invoice_id: int) -> InvoiceRecord:
        invoice = self.invoice_store.get(invoice_id)
        if invoice is None:
            raise InvoiceLifecycleError(f"Invoice {invoice_id} was not found.")
        return invoice

    def _move_pdf_in_sharepoint(
        self,
        invoice: InvoiceRecord,
        destination_folder: str,
        destination_filename: str,
    ) -> None:
        if self.sharepoint_client is None:
            self.activity_feed.add_event(
                event_type="sharepoint_pending",
                target_role=ROLE_PURCHASE_LEDGER,
                message=(
                    "SharePoint is not configured; the PDF was routed but not "
                    "moved. Set SHAREPOINT_SITE_ID / SHAREPOINT_DRIVE_ID to "
                    "enable filing."
                ),
                invoice_id=invoice.id,
            )
            return
        try:
            pdf_bytes = Path(invoice.stored_path).read_bytes()
            item = self.sharepoint_client.upload_and_move(
                filename=invoice.original_filename,
                content=pdf_bytes,
                destination_folder=destination_folder,
                destination_filename=destination_filename,
            )
            self.invoice_store.update_fields(
                invoice.id,
                sharepoint_item_id=str(item.get("id", "")),
                sharepoint_web_url=self.sharepoint_client.get_item_web_url(item),
            )
        except SharePointError as error:
            self.activity_feed.add_event(
                event_type="sharepoint_error",
                target_role=ROLE_PURCHASE_LEDGER,
                message=f"SharePoint move failed for invoice {invoice.id}: {error}",
                invoice_id=invoice.id,
            )
