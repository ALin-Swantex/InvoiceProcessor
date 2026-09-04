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
from app.approval_matrix import ApprovalMatrixStore
from app.approval_matrix import find_approvers as _default_find_approvers
from app.companies import CompanyStore
from app.companies import get_company as _default_get_company
from app.duplicates import find_possible_duplicate
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

    `companies_store` / `approval_matrix_store` are optional. When omitted,
    the module-level default stores (the process-wide singletons in
    app/companies.py and app/approval_matrix.py) are used, which is fine for
    a single running server. Pass explicit stores (e.g. the same instances
    attached to app.state) when isolation matters, such as in tests, so
    admin edits made through /api/admin/* are immediately visible here
    instead of going through a different singleton bound to a different
    database file.
    """

    def __init__(
        self,
        invoice_store: InvoiceStore,
        irj_generator: IrjNumberGenerator,
        activity_feed: ActivityFeedStore,
        sharepoint_client: SharePointClient | None,
        companies_store: CompanyStore | None = None,
        approval_matrix_store: ApprovalMatrixStore | None = None,
    ) -> None:
        self.invoice_store = invoice_store
        self.irj_generator = irj_generator
        self.activity_feed = activity_feed
        self.sharepoint_client = sharepoint_client
        self.companies_store = companies_store
        self.approval_matrix_store = approval_matrix_store

    def _get_company(self, name: str):
        if self.companies_store is not None:
            return self.companies_store.get(name)
        return _default_get_company(name)

    def _find_approvers(self, company: str, supplier: str):
        if self.approval_matrix_store is not None:
            return self.approval_matrix_store.find(company, supplier)
        return _default_find_approvers(company, supplier)

    # ------------------------------------------------------------------
    # Stage 1: AI extraction (placeholder)
    # ------------------------------------------------------------------

    def run_extraction(self, invoice_id: int) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        # SOFTWARE_SPEC.md section 13 ("Exceptions and Errors") lists "the
        # PDF cannot be read" as a scenario the system must handle sensibly
        # rather than crash on. Once real AI extraction (Azure Document
        # Intelligence) is wired into run_ai_extraction, a corrupt/unreadable
        # PDF should flag the invoice for review, not raise an unhandled
        # error and stall the pipeline.
        try:
            result = run_ai_extraction(Path(invoice.stored_path))
        except Exception as error:
            record = self.invoice_store.update_fields(
                invoice_id,
                status="Needs Review",
                review_reason=f"The PDF could not be read automatically: {error}",
            )
            self.activity_feed.add_event(
                event_type="needs_review",
                target_role=ROLE_PURCHASE_LEDGER,
                message=(
                    f"Invoice {invoice.original_filename} could not be read "
                    "and needs manual review."
                ),
                invoice_id=invoice_id,
            )
            return record
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
        override_duplicate: bool = False,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        company_profile = self._get_company(company)
        if company_profile is None:
            raise InvoiceLifecycleError(
                f"'{company}' is not a recognised company. Add it to app/companies.py "
                "(or the production Companies SharePoint List) first."
            )

        # Stage 6 (GENERAL_PROCESS.md): duplicate detection on
        # Company + Supplier + Supplier Invoice Number. A possible duplicate
        # is NEVER silently dropped or auto-merged -- it is flagged for
        # Purchase Ledger, who can then set override_duplicate=True (via the
        # "This is not a duplicate" action) once they have manually
        # confirmed it is a genuinely separate invoice.
        if not override_duplicate:
            duplicate = find_possible_duplicate(
                self.invoice_store,
                exclude_invoice_id=invoice_id,
                company=company,
                supplier=supplier,
                supplier_invoice_number=supplier_invoice_number,
            )
            if duplicate is not None:
                record = self.invoice_store.update_fields(
                    invoice_id,
                    company=company,
                    supplier=supplier,
                    supplier_invoice_number=supplier_invoice_number,
                    po_number=purchase_order_number or None,
                    invoice_date=invoice_date,
                    invoice_value=invoice_value,
                    currency=currency,
                    status="Needs Review",
                    duplicate_of_invoice_id=duplicate.invoice_id,
                    review_reason=(
                        f"Possible duplicate of invoice #{duplicate.invoice_id} "
                        f"(IRJ {duplicate.irj_number or 'not yet assigned'}, "
                        f"status {duplicate.status}), matched on "
                        f"{duplicate.match_basis}. Purchase Ledger must "
                        "confirm this is a genuinely separate invoice before it "
                        "can be routed."
                    ),
                )
                self.activity_feed.add_event(
                    event_type="possible_duplicate",
                    target_role=ROLE_PURCHASE_LEDGER,
                    message=(
                        f"Invoice {invoice.original_filename} looks like a possible "
                        f"duplicate of invoice #{duplicate.invoice_id} "
                        f"(matched on {duplicate.match_basis})."
                    ),
                    invoice_id=invoice_id,
                )
                return record

        irj_number = invoice.irj_number or self.irj_generator.generate()
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
            "duplicate_of_invoice_id": None,
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

        record = self.invoice_store.update_fields(
            invoice_id,
            **base_fields,
            invoice_type="nominal",
            status="Awaiting Sage Registration",
            review_reason=None,
        )
        self.activity_feed.add_event(
            event_type="sage_registration_pending",
            target_role=ROLE_PURCHASE_LEDGER,
            message=f"Invoice {irj_number} ({supplier}) is awaiting Sage registration.",
            invoice_id=invoice_id,
        )
        return record

    # ------------------------------------------------------------------
    # Stage 3a: PO matching
    # ------------------------------------------------------------------

    def record_po_match(
        self,
        invoice_id: int,
        *,
        matched: bool,
        notes: str | None,
        query_category: str | None = None,
        purchasing_contact: str | None = None,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status not in ("Awaiting PO Matching", "PO Query / Matching Issue"):
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is not awaiting PO matching (status: {invoice.status})."
            )
        if matched:
            record = self.invoice_store.update_fields(
                invoice_id,
                status="Awaiting Sage Registration",
                po_query_notes=notes,
                po_query_category=None,
                po_query_contact=None,
            )
            self.activity_feed.add_event(
                event_type="sage_registration_pending",
                target_role=ROLE_PURCHASE_LEDGER,
                message=(
                    f"Invoice {invoice.irj_number} matched against its PO and is "
                    "awaiting Sage registration."
                ),
                invoice_id=invoice_id,
            )
            return record

        # GENERAL_PROCESS.md "Matching issue": Purchase Ledger records a
        # query category, details, and the Purchasing contact who will
        # investigate. The invoice stays outstanding -- it must not be
        # registered in Sage or moved to Approved -- until the query is
        # resolved and record_po_match is called again with matched=True.
        record = self.invoice_store.update_fields(
            invoice_id,
            status="PO Query / Matching Issue",
            po_query_notes=notes,
            po_query_category=query_category,
            po_query_contact=purchasing_contact,
        )
        self.activity_feed.add_event(
            event_type="po_query",
            target_role=ROLE_PURCHASING,
            message=(
                f"Invoice {invoice.irj_number}: PO matching issue"
                f"{f' ({query_category})' if query_category else ''} — {notes or 'see notes'}."
            ),
            invoice_id=invoice_id,
        )
        return record

    def register_in_sage(
        self,
        invoice_id: int,
        *,
        sage_reference: str,
        recorded_by: str,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        retrying_missing_route = (
            invoice.status == "Needs Review"
            and invoice.invoice_type == "nominal"
            and invoice.sage_registered_at is not None
            and invoice.approver1_email is None
        )
        if invoice.status != "Awaiting Sage Registration" and not retrying_missing_route:
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is not awaiting Sage registration "
                f"(status: {invoice.status})."
            )
        if not sage_reference.strip():
            raise InvoiceLifecycleError("A Sage registration reference is required.")

        now = datetime.now(timezone.utc).isoformat()
        if not retrying_missing_route:
            invoice = self.invoice_store.update_fields(
                invoice_id,
                sage_registered_at=now,
                sage_reference=sage_reference.strip(),
                sage_registered_by=recorded_by,
            )

        if invoice.invoice_type == "po":
            record = self.invoice_store.update_fields(
                invoice_id, status="Approved", review_reason=None
            )
            self.activity_feed.add_event(
                event_type="approved",
                target_role=ROLE_PURCHASE_LEDGER,
                message=(
                    f"Invoice {invoice.irj_number} registered in Sage and approved "
                    "for payment."
                ),
                invoice_id=invoice_id,
            )
            return record

        entry = self._find_approvers(str(invoice.company), str(invoice.supplier))
        if entry is None:
            record = self.invoice_store.update_fields(
                invoice_id,
                status="Needs Review",
                review_reason=(
                    f"No approval matrix entry found for supplier '{invoice.supplier}' "
                    f"under '{invoice.company}'. Configure the route, then retry."
                ),
            )
            self.activity_feed.add_event(
                event_type="needs_review",
                target_role=ROLE_PURCHASE_LEDGER,
                message=(
                    f"Invoice {invoice.irj_number}: no approver configured for "
                    f"'{invoice.supplier}' under '{invoice.company}'."
                ),
                invoice_id=invoice_id,
            )
            return record

        record = self.invoice_store.update_fields(
            invoice_id,
            status="Awaiting Approval 1",
            approver1_name=entry.approver1.name,
            approver1_email=entry.approver1.email,
            approver2_name=entry.approver2.name if entry.approver2 else None,
            approver2_email=entry.approver2.email if entry.approver2 else None,
            review_reason=None,
        )
        send_email_notification(
            recipient=entry.approver1.email,
            subject=f"Invoice {invoice.irj_number} awaiting your approval",
            body=(
                f"Invoice {invoice.irj_number} from {invoice.supplier} requires "
                "your approval."
            ),
        )
        self.activity_feed.add_event(
            event_type="approval_pending",
            target_role=ROLE_APPROVER_1,
            message=(
                f"Invoice {invoice.irj_number} ({invoice.supplier}) is awaiting "
                "Approver 1 approval."
            ),
            invoice_id=invoice_id,
        )
        return record

    def reject_invoice(
        self, invoice_id: int, *, reason: str, recorded_by: str
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status not in (
            "Awaiting PO Matching",
            "PO Query / Matching Issue",
        ):
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} cannot be rejected from status {invoice.status}."
            )
        if not reason.strip():
            raise InvoiceLifecycleError("A rejection reason is required.")
        record = self.invoice_store.update_fields(
            invoice_id,
            status="Rejected",
            rejection_reason=reason.strip(),
        )
        self.activity_feed.add_event(
            event_type="rejected",
            target_role=ROLE_PURCHASE_LEDGER,
            message=(
                f"Invoice {invoice.irj_number} rejected by {recorded_by}: "
                f"{reason.strip()}"
            ),
            invoice_id=invoice_id,
        )
        return record

    def cancel_confirmed_duplicate(
        self, invoice_id: int, *, recorded_by: str
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if (
            invoice.status != "Needs Review"
            or invoice.duplicate_of_invoice_id is None
            or invoice.sage_registered_at is not None
        ):
            raise InvoiceLifecycleError(
                "Only an invoice flagged as a possible duplicate can be cancelled "
                "as a confirmed duplicate before Sage registration."
            )
        now = datetime.now(timezone.utc).isoformat()
        reason = f"Confirmed duplicate of invoice #{invoice.duplicate_of_invoice_id}."
        record = self.invoice_store.update_fields(
            invoice_id,
            status="Cancelled - Duplicate",
            cancelled_at=now,
            cancelled_by=recorded_by,
            cancellation_reason=reason,
        )
        self.activity_feed.add_event(
            event_type="duplicate_cancelled",
            target_role=ROLE_PURCHASE_LEDGER,
            message=f"Invoice {invoice.id} cancelled: {reason}",
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
        if decision not in ("approved", "rejected", "on_hold"):
            raise InvoiceLifecycleError(
                "Decision must be 'approved', 'rejected', or 'on_hold'."
            )

        invoice = self._require_invoice(invoice_id)
        expected_status = f"Awaiting Approval {level}"
        if invoice.status != expected_status:
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is not {expected_status} (status: {invoice.status})."
            )

        now = datetime.now(timezone.utc).isoformat()

        if decision == "on_hold":
            # SOFTWARE_SPEC.md section 8: approvers can place an invoice on
            # hold with a required comment (e.g. "price under query", "waiting
            # for a credit note") so Purchase Ledger can see why approval is
            # delayed without having to chase the approver separately. The
            # invoice never proceeds automatically from here -- Purchase
            # Ledger must call resume_approval once the issue is resolved.
            if not comments:
                raise InvoiceLifecycleError(
                    "A comment explaining the hold is required."
                )
            fields = {
                "status": "Approval Query / On Hold",
                "hold_level": level,
                "hold_reason": comments,
                f"approver{level}_comments": comments,
            }
            record = self.invoice_store.update_fields(invoice_id, **fields)
            self.activity_feed.add_event(
                event_type="approval_on_hold",
                target_role=ROLE_PURCHASE_LEDGER,
                message=(
                    f"Invoice {invoice.irj_number}: Approver {level} placed it "
                    f"on hold — {comments}"
                ),
                invoice_id=invoice_id,
            )
            return record

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
        # SOFTWARE_SPEC.md section 9: "Once fully approved... The Purchase
        # Ledger team should receive an email notification where
        # appropriate." Purchase Ledger isn't the one clicking approve here
        # (an approver is), so -- unlike the PO-matched branch above, where
        # Purchase Ledger performed the action themselves -- they need an
        # actual email, not just an activity feed entry they'd have to go
        # looking for.
        send_email_notification(
            recipient="purchase-ledger@example.test",
            subject=f"Invoice {invoice.irj_number} fully approved",
            body=(
                f"Invoice {invoice.irj_number} ({invoice.supplier}) has completed "
                "all required nominal approvals and is ready for payment."
            ),
        )
        self.activity_feed.add_event(
            event_type="approved",
            target_role=ROLE_PURCHASE_LEDGER,
            message=f"Invoice {invoice.irj_number} fully approved and ready for payment.",
            invoice_id=invoice_id,
        )
        return record

    def resume_approval(self, invoice_id: int, *, resolution_notes: str | None) -> InvoiceRecord:
        """Purchase Ledger resumes an invoice that an approver placed on
        hold, once the underlying question has been resolved. This always
        returns the invoice to the same approval level it was held at --
        never auto-approves it."""
        invoice = self._require_invoice(invoice_id)
        if invoice.status != "Approval Query / On Hold":
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is not on hold (status: {invoice.status})."
            )
        level = invoice.hold_level or 1
        record = self.invoice_store.update_fields(
            invoice_id,
            status=f"Awaiting Approval {level}",
            hold_reason=(
                f"{invoice.hold_reason or ''} — resolved by Purchase Ledger: "
                f"{resolution_notes}"
                if resolution_notes
                else invoice.hold_reason
            ),
        )
        self.activity_feed.add_event(
            event_type="approval_resumed",
            target_role=ROLE_APPROVER_1 if level == 1 else ROLE_APPROVER_2,
            message=(
                f"Invoice {invoice.irj_number}: hold resolved, awaiting "
                f"Approver {level} again."
            ),
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
        self,
        invoice_id: int,
        *,
        payment_date: str,
        payment_reference: str | None,
        payment_method: str | None = None,
        recorded_by: str | None = None,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status != "Approved":
            raise InvoiceLifecycleError(
                f"Only Approved invoices can be marked as paid (status: {invoice.status})."
            )
        if not payment_method or not payment_method.strip():
            raise InvoiceLifecycleError("A payment method is required.")
        if not payment_reference or not payment_reference.strip():
            raise InvoiceLifecycleError("A payment reference is required.")
        if not payment_date.strip():
            raise InvoiceLifecycleError("A payment date is required.")
        now = datetime.now(timezone.utc).isoformat()
        # SOFTWARE_SPEC.md section 10: "Doing this should automatically:
        # Record who marked the invoice as paid".
        record = self.invoice_store.update_fields(
            invoice_id,
            status="Paid / Awaiting Bank Reconciliation",
            payment_date=payment_date,
            payment_reference=payment_reference,
            payment_method=payment_method.strip(),
            paid_by=recorded_by,
            is_foreign_payment=0,
            payment_route_decided_at=now,
            payment_route_decided_by=recorded_by,
            foreign_allocation_date=None,
            foreign_allocation_reference=None,
            foreign_allocated_by=None,
        )
        self.activity_feed.add_event(
            event_type="paid",
            target_role=ROLE_PURCHASE_LEDGER,
            message=f"Invoice {invoice.irj_number} paid; awaiting bank reconciliation.",
            invoice_id=invoice_id,
        )
        return record

    def route_as_foreign_payment(
        self, invoice_id: int, *, recorded_by: str
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status != "Approved":
            raise InvoiceLifecycleError(
                f"Only Approved invoices can be marked as foreign payments "
                f"(status: {invoice.status})."
            )
        record = self.invoice_store.update_fields(
            invoice_id,
            status="Foreign Payment / Awaiting Allocation",
            is_foreign_payment=1,
            payment_route_decided_at=datetime.now(timezone.utc).isoformat(),
            payment_route_decided_by=recorded_by,
            payment_date=None,
            payment_reference=None,
            payment_method=None,
            paid_by=None,
            reconciliation_date=None,
            reconciliation_notes=None,
            reconciled_by=None,
        )
        self.activity_feed.add_event(
            event_type="foreign_payment_pending",
            target_role=ROLE_PURCHASE_LEDGER,
            message=(
                f"Invoice {invoice.irj_number} marked as a foreign payment by "
                f"{recorded_by}; awaiting allocation."
            ),
            invoice_id=invoice_id,
        )
        return record

    def mark_foreign_allocated(
        self,
        invoice_id: int,
        *,
        allocation_date: str,
        allocation_reference: str,
        recorded_by: str,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status != "Foreign Payment / Awaiting Allocation":
            raise InvoiceLifecycleError(
                f"Only foreign payments awaiting allocation can be allocated "
                f"(status: {invoice.status})."
            )
        if not allocation_reference.strip():
            raise InvoiceLifecycleError("An allocation reference is required.")
        if not allocation_date.strip():
            raise InvoiceLifecycleError("An allocation date is required.")
        record = self.invoice_store.update_fields(
            invoice_id,
            status="Reconciled / Complete",
            foreign_allocation_date=allocation_date,
            foreign_allocation_reference=allocation_reference.strip(),
            foreign_allocated_by=recorded_by,
        )
        self.activity_feed.add_event(
            event_type="foreign_payment_allocated",
            target_role=ROLE_PURCHASE_LEDGER,
            message=(
                f"Foreign payment for invoice {invoice.irj_number} allocated and "
                "completed."
            ),
            invoice_id=invoice_id,
        )
        return record

    def revert_foreign_payment_route(
        self, invoice_id: int, *, recorded_by: str
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status != "Foreign Payment / Awaiting Allocation":
            raise InvoiceLifecycleError(
                f"Only a foreign payment awaiting allocation can be returned to "
                f"domestic payment (status: {invoice.status})."
            )
        record = self.invoice_store.update_fields(
            invoice_id,
            status="Approved",
            is_foreign_payment=None,
            payment_route_decided_at=None,
            payment_route_decided_by=None,
            foreign_allocation_date=None,
            foreign_allocation_reference=None,
            foreign_allocated_by=None,
        )
        self.activity_feed.add_event(
            event_type="foreign_payment_reverted",
            target_role=ROLE_PURCHASE_LEDGER,
            message=(
                f"Invoice {invoice.irj_number} returned to domestic payment by "
                f"{recorded_by}."
            ),
            invoice_id=invoice_id,
        )
        return record

    def mark_reconciled(
        self,
        invoice_id: int,
        *,
        reconciliation_date: str,
        notes: str | None,
        recorded_by: str | None = None,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status != "Paid / Awaiting Bank Reconciliation":
            raise InvoiceLifecycleError(
                f"Only invoices awaiting bank reconciliation can be reconciled "
                f"(status: {invoice.status})."
            )
        # SOFTWARE_SPEC.md section 11: "The system should record: ...Who
        # completed the reconciliation, where practical".
        record = self.invoice_store.update_fields(
            invoice_id,
            status="Reconciled / Complete",
            reconciliation_date=reconciliation_date,
            reconciliation_notes=notes,
            reconciled_by=recorded_by,
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
