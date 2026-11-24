"""Stage 2: Purchase Ledger confirmation, IRJ numbering, PO matching and Sage registration."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os

from app.activity_feed import ROLE_APPROVER_1, ROLE_PURCHASING
from app.company_folders import REJECTED_INVOICES_FOLDER
from app.duplicates import duplicate_reference, find_possible_duplicate
from app.invoices import InvoiceRecord
from app.invoice_number_validation import invoice_number_warnings
from app.workflow import (
    ConfirmedInvoice,
    RoutingValidationError,
    route_confirmed_invoice,
)
from app.statuses import Status
from app.lifecycle.common import (
    InvoiceLifecycleError,
    _append_history_entry,
    _is_unique_violation,
)
from app.lifecycle.base import LifecycleBase


class ConfirmationStage(LifecycleBase):
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
        recorded_by: str = "Purchase Ledger",
        correction_reason: str | None = None,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        # Confirming routes the invoice from the start again; doing that to
        # one already in approval or payment (stale tab, double submit) would
        # rewind it and move its PDF.
        if invoice.status not in (Status.NEEDS_REVIEW, Status.AWAITING_EXTRACTION):
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is not awaiting confirmation "
                f"(status: {invoice.status})."
            )
        if invoice.document_type != "invoice":
            raise InvoiceLifecycleError(
                "A supplier statement cannot enter the invoice workflow. "
                "File it to its company statement folder or mark it as an invoice."
            )
        company_profile = self._get_company(company)
        if company_profile is None:
            raise InvoiceLifecycleError(
                f"'{company}' is not a recognised company. Add it to app/companies.py "
                "(or the production Companies SharePoint List) first."
            )
        supplier_profile = self._get_supplier(supplier)
        company = company_profile.name
        if supplier_profile is not None:
            supplier = supplier_profile.name
        submitted_fields = {
            "company": company,
            "supplier": supplier,
            "supplier_invoice_number": supplier_invoice_number,
            "po_number": purchase_order_number or None,
            "invoice_date": invoice_date,
            "invoice_value": invoice_value,
            "currency": currency,
        }
        original_fields: dict[str, object] = {}
        if invoice.extracted_fields_json:
            try:
                parsed = json.loads(invoice.extracted_fields_json)
                if isinstance(parsed, dict):
                    original_fields = parsed
            except json.JSONDecodeError:
                original_fields = {}
        corrected_fields = {
            key: {"original": original_fields.get(key), "corrected": value}
            for key, value in submitted_fields.items()
            if original_fields and original_fields.get(key) != value
        }
        review_metadata: dict[str, object] = {
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "reviewed_by": recorded_by,
            "corrected_fields_json": (
                json.dumps(corrected_fields, sort_keys=True, separators=(",", ":"))
                if corrected_fields
                else None
            ),
            "correction_reason": (
                correction_reason.strip()
                if correction_reason and correction_reason.strip()
                else "Purchase Ledger corrected extracted invoice fields."
                if corrected_fields
                else None
            ),
        }
        number_warnings = invoice_number_warnings(
            supplier_invoice_number,
            supplier=supplier,
            pattern=(
                supplier_profile.invoice_number_pattern
                if supplier_profile is not None
                else None
            ),
        )
        if number_warnings:
            raise InvoiceLifecycleError(" ".join(number_warnings))

        # Claim the invoice before allocating an IRJ, so a double submit (or
        # a manual confirm racing auto-confirm) cannot allocate two numbers.
        with self._claimed(
            invoice,
            Status.PROCESSING_CONFIRMATION,
            refusal=f"Invoice {invoice_id} is not awaiting confirmation",
        ) as save_claimed:
            # Duplicate detection on
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
                    reference = duplicate_reference(
                        duplicate.invoice_id, duplicate.irj_number
                    )
                    self._move_to_flagged(invoice)
                    record = save_claimed(
                        company=company,
                        supplier=supplier,
                        supplier_invoice_number=supplier_invoice_number,
                        po_number=purchase_order_number or None,
                        invoice_date=invoice_date,
                        invoice_value=invoice_value,
                        currency=currency,
                        status=Status.NEEDS_REVIEW,
                        review_return_status=None,
                        duplicate_of_invoice_id=duplicate.invoice_id,
                        review_reason=(
                            f"It's a duplicate of {reference} "
                            f"(status {duplicate.status}), matched on "
                            f"{duplicate.match_basis}. Purchase Ledger must "
                            "confirm this is a genuinely separate invoice before it "
                            "can be routed."
                        ),
                        routing_explanation=(
                            "Routing paused because the company, supplier and invoice "
                            "number match an existing invoice."
                        ),
                        **review_metadata,
                    )
                    self._log_purchase_ledger(
                        event_type="possible_duplicate",
                        message=(
                            f"Invoice {invoice.original_filename} looks like a possible "
                            f"duplicate of {reference} "
                            f"(matched on {duplicate.match_basis})."
                        ),
                        invoice_id=invoice_id,
                    )
                    return record

            manual_irj = self._uses_manual_irj(company_profile.name)
            irj_number = (
                invoice.irj_number
                if self._irj_belongs_to_company(invoice, company_profile.name)
                else None
                if manual_irj
                else self.irj_generator.generate(company_profile.name)
            )
            if irj_number and irj_number != invoice.irj_number:
                # Record the allocation straight away. If routing or SharePoint
                # filing fails below, a retry reuses this number instead of
                # allocating another one and leaving a gap in the sequence.
                invoice = save_claimed(
                    irj_number=irj_number, company=company_profile.name
                )
            try:
                decision = route_confirmed_invoice(
                    ConfirmedInvoice(
                        invoice_id=str(invoice_id),
                        company=company,
                        company_folder=company_profile.company_folder,
                        original_filename=invoice.original_filename,
                        irj_number=irj_number,
                        defer_irj=manual_irj,
                        purchase_order_number=purchase_order_number,
                        po_matching_folder=company_profile.po_matching_folder,
                        purchase_ledger_recipient=os.environ.get(
                            "PURCHASE_LEDGER_NOTIFICATION_EMAIL",
                            "purchase-ledger@example.test",
                        ),
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
                "review_return_status": None,
                **review_metadata,
            }

            if decision.route == "purchase_order":
                record = save_claimed(
                    **base_fields,
                    invoice_type="po",
                    status=decision.status,
                    review_reason=None,
                    routing_explanation=(
                        f"PO number {purchase_order_number} was confirmed by "
                        f"{recorded_by}; routed to PO Matching."
                    ),
                )
                self._send_stage_email(
                    invoice_id,
                    "po_matching",
                    recipient=str(decision.notification_recipient),
                    subject=(
                        f"New invoice ({invoice.original_filename}) waiting for "
                        "PO matching"
                    ),
                    body=(
                        f"New invoice ({invoice.original_filename}) is waiting for "
                        f"PO matching. IRJ: {irj_number or 'assigned at Sage registration'}. PO: "
                        f"{purchase_order_number}."
                    ),
                )
                self._log_purchase_ledger(
                    event_type="po_awaiting_match",
                    message=(
                        f"Invoice {irj_number or invoice.original_filename} ({supplier}) is awaiting PO matching "
                        f"against PO {purchase_order_number}."
                    ),
                    invoice_id=invoice_id,
                )
                return record

            record = save_claimed(
                **base_fields,
                invoice_type="nominal",
                status=Status.AWAITING_SAGE_REGISTRATION,
                review_reason=None,
                routing_explanation=(
                    f"No PO number was confirmed by {recorded_by}; routed as a "
                    "nominal invoice to Sage Registration."
                ),
            )
            self._log_purchase_ledger(
                event_type="sage_registration_pending",
                message=f"Invoice {irj_number} ({supplier}) is awaiting Sage registration.",
                invoice_id=invoice_id,
            )
            return record

    def record_po_match(
        self,
        invoice_id: int,
        *,
        matched: bool,
        notes: str | None,
        query_category: str | None = None,
        purchasing_contact: str | None = None,
        recorded_by: str = "Purchase Ledger",
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status not in (Status.AWAITING_PO_MATCHING, Status.PO_QUERY):
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is not awaiting PO matching (status: {invoice.status})."
            )
        if matched:
            po_history = invoice.po_query_notes
            if po_history:
                po_history = _append_history_entry(
                    po_history,
                    f"{recorded_by} resolved PO query / matched",
                    notes or "Matched without additional resolution notes.",
                )
            # A queried invoice goes back to the PO matching folder; a matched
            # one is already there.
            record = self._transition(
                invoice,
                Status.AWAITING_SAGE_REGISTRATION,
                folder=self._company_folders(invoice).po_match,
                move_pdf=invoice.status == Status.PO_QUERY,
                po_query_notes=po_history or notes,
                po_query_category=None,
                po_query_contact=None,
            )
            self._log_purchase_ledger(
                event_type="sage_registration_pending",
                message=(
                    f"Invoice {invoice.irj_number} matched against its PO and is "
                    "awaiting Sage registration."
                ),
                invoice_id=invoice_id,
            )
            return record

        # Recording a query is not a workflow decision. Keep both the
        # workflow stage and SharePoint location unchanged until the invoice
        # is explicitly matched or rejected.
        query_context = " · ".join(
            value
            for value in (
                f"Category: {query_category}" if query_category else None,
                f"Contact: {purchasing_contact}" if purchasing_contact else None,
            )
            if value
        )
        query_details = notes or "No additional details supplied."
        if query_context:
            query_details = f"{query_details}\n{query_context}"
        record = self.invoice_store.update_fields(
            invoice_id,
            po_query_notes=_append_history_entry(
                invoice.po_query_notes,
                f"{recorded_by} recorded PO query",
                query_details,
            ),
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
        irj_number: str,
        recorded_by: str,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        retrying_missing_route = (
            invoice.status == Status.NEEDS_REVIEW
            and invoice.invoice_type == "nominal"
            and invoice.sage_registered_at is not None
            and not invoice.approver1_email
        )
        if invoice.status != Status.AWAITING_SAGE_REGISTRATION and not retrying_missing_route:
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is not awaiting Sage registration "
                f"(status: {invoice.status})."
            )
        normalized_irj = irj_number.strip()
        if len(normalized_irj) != 6 or not normalized_irj.isdigit():
            raise InvoiceLifecycleError(
                "The IRJ number must contain exactly six digits."
            )
        if retrying_missing_route and normalized_irj != (invoice.irj_number or ""):
            raise InvoiceLifecycleError(
                f"The IRJ number cannot be changed on retry; invoice {invoice_id} "
                f"was registered in Sage as IRJ {invoice.irj_number}."
            )
        existing = self.invoice_store.get_by_irj_number(
            normalized_irj, str(invoice.company)
        )
        if existing is not None and existing.id != invoice_id:
            raise InvoiceLifecycleError(
                f"IRJ number {normalized_irj} is already assigned to invoice "
                f"{existing.id}."
            )
        if (
            not self._uses_manual_irj(str(invoice.company))
            and invoice.irj_number
            and normalized_irj != invoice.irj_number
        ):
            raise InvoiceLifecycleError(
                f"IRJ {invoice.irj_number} was allocated automatically for "
                f"{invoice.company} and cannot be changed."
            )

        now = datetime.now(timezone.utc).isoformat()
        if not retrying_missing_route:
            try:
                invoice = self.invoice_store.update_fields(
                    invoice_id,
                    irj_number=normalized_irj,
                    sage_registered_at=now,
                    sage_reference=None,
                    sage_registered_by=recorded_by,
                )
            except Exception as error:
                # Two requests can pass the check above together; the unique
                # (company, IRJ) index then rejects the second. Report that as
                # a normal lifecycle error, not an unhandled database error.
                if _is_unique_violation(error):
                    raise InvoiceLifecycleError(
                        f"IRJ number {normalized_irj} is already assigned to "
                        "another invoice."
                    ) from error
                raise

        if invoice.invoice_type == "po":
            record = self._transition(invoice, Status.APPROVED, review_reason=None)
            self._log_purchase_ledger(
                event_type="approved",
                message=(
                    f"Invoice {invoice.irj_number} registered in Sage and approved "
                    "for payment."
                ),
                invoice_id=invoice_id,
            )
            return self.auto_route_for_payment(record.id)

        entry = self._find_approvers(str(invoice.company), str(invoice.supplier))
        missing_route_detail = None
        if entry is None:
            missing_route_detail = (
                f"No approval matrix entry found for supplier '{invoice.supplier}' "
                f"under '{invoice.company}'."
            )
        else:
            missing_recipients = [
                f"Approver 1 ({entry.approver1.name})"
                if not entry.approver1.email.strip()
                else None,
                (
                    f"Approver 2 ({entry.approver2.name})"
                    if entry.approver2 is not None
                    and not entry.approver2.email.strip()
                    else None
                ),
            ]
            missing_recipients = [
                recipient for recipient in missing_recipients if recipient
            ]
            if missing_recipients:
                missing_route_detail = (
                    "The approval route is missing an email address for "
                    f"{', '.join(missing_recipients)}."
                )
        if missing_route_detail is not None:
            with self._claimed(invoice) as save:
                self._move_to_flagged(invoice)
                record = save(
                    status=Status.NEEDS_REVIEW,
                    review_return_status=None,
                    review_reason=f"{missing_route_detail} Configure the route, then retry.",
                )
            self._log_purchase_ledger(
                event_type="needs_review",
                message=(
                    f"Invoice {invoice.irj_number}: {missing_route_detail} "
                    "Purchase Ledger must update the approval matrix."
                ),
                invoice_id=invoice_id,
            )
            return record

        record = self._transition(
            invoice,
            Status.AWAITING_APPROVAL_1,
            approver1_name=entry.approver1.name,
            approver1_email=entry.approver1.email,
            approver2_name=entry.approver2.name if entry.approver2 else None,
            approver2_email=entry.approver2.email if entry.approver2 else None,
            review_reason=None,
            approval_requested_at=now,
            approval_reminder_sent_at=None,
            approval_hold_started_at=None,
            approval_hold_reminder_sent_at=None,
        )
        self._send_stage_email(
            invoice_id,
            "approval_1",
            recipient=entry.approver1.email,
            subject=(
                f"New invoice ({invoice.original_filename}) waiting for approval"
            ),
            body=self._approval_email_body(record, 1, reminder=False),
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

    def _irj_belongs_to_company(
        self, invoice: InvoiceRecord, company_name: str
    ) -> bool:
        """True when the invoice's existing IRJ came from this company's sequence.

        IRJ numbers are allocated per company, so a number issued for one company
        must not be reused if the invoice is re-confirmed under another.
        """
        if not invoice.irj_number:
            return False
        if not invoice.company:
            return True
        existing = self._get_company(invoice.company)
        existing_name = existing.name if existing is not None else invoice.company
        return existing_name.strip().casefold() == company_name.strip().casefold()

    def _uses_manual_irj(self, company: str) -> bool:
        default = (
            "manual"
            if company.strip().casefold() in {"swan", "cel"}
            else "automatic"
        )
        if self.configuration_getter is None:
            return default == "manual"
        mode = self.configuration_getter(
            f"irj_mode:{company.strip().casefold()}", default
        )
        return str(mode).strip().casefold() == "manual"

    def reject_invoice(
        self, invoice_id: int, *, reason: str, recorded_by: str
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status not in (
            Status.AWAITING_PO_MATCHING,
            Status.PO_QUERY,
        ):
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} cannot be rejected from status {invoice.status}."
            )
        if not reason.strip():
            raise InvoiceLifecycleError("A rejection reason is required.")
        record = self._transition(
            invoice,
            Status.REJECTED,
            folder=REJECTED_INVOICES_FOLDER,
            filename=self._rejected_filename(invoice),
            rejection_reason=reason.strip(),
        )
        self._log_purchase_ledger(
            event_type="rejected",
            message=(
                f"Invoice {invoice.irj_number} rejected by {recorded_by}: "
                f"{reason.strip()}"
            ),
            invoice_id=invoice_id,
        )
        self._notify_purchase_ledger_rejection(record)
        return record

    def cancel_confirmed_duplicate(
        self, invoice_id: int, *, recorded_by: str
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if (
            invoice.status != Status.NEEDS_REVIEW
            or invoice.duplicate_of_invoice_id is None
            or invoice.sage_registered_at is not None
        ):
            raise InvoiceLifecycleError(
                "Only an invoice flagged as a possible duplicate can be cancelled "
                "as a confirmed duplicate before Sage registration."
            )
        now = datetime.now(timezone.utc).isoformat()
        original = self.invoice_store.get(invoice.duplicate_of_invoice_id)
        reference = duplicate_reference(
            invoice.duplicate_of_invoice_id,
            original.irj_number if original is not None else None,
        )
        reason = f"Confirmed duplicate of {reference}."
        record = self._transition(
            invoice,
            Status.CANCELLED_DUPLICATE,
            folder=REJECTED_INVOICES_FOLDER,
            filename=self._rejected_filename(invoice),
            cancelled_at=now,
            cancelled_by=recorded_by,
            cancellation_reason=reason,
        )
        self._log_purchase_ledger(
            event_type="duplicate_cancelled",
            message=f"Invoice {invoice.id} cancelled: {reason}",
            invoice_id=invoice_id,
        )
        return record
