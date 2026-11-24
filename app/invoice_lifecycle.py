from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from typing import Callable, Iterator
from zoneinfo import ZoneInfo

from app.activity_feed import (
    ROLE_APPROVER_1,
    ROLE_APPROVER_2,
    ROLE_PURCHASE_LEDGER,
    ROLE_PURCHASING,
    ActivityFeedStore,
)
from app.ai_extraction import (
    ExtractionConfigurationError,
    ExtractionResult,
    ExtractionServiceError,
    ReferenceData,
    run_ai_extraction,
)
from app.approval_matrix import ApprovalMatrixStore
from app.approval_matrix import find_approvers as _default_find_approvers
from app.companies import CompanyStore
from app.companies import get_company as _default_get_company
from app.companies import list_companies as _default_list_companies
from app.company_folders import (
    FLAGGED_INVOICES_FOLDER,
    REJECTED_INVOICES_FOLDER,
    CompanyFolderStructure,
    statement_company_folder,
)
from app.document_classification import (
    DocumentClassification,
    classify_pdf_document,
)
from app.duplicates import duplicate_reference, find_possible_duplicate
from app.email_notifications import send_email_notification
from app.invoices import InvoiceRecord, InvoiceStore
from app.invoice_number_validation import invoice_number_warnings
from app.irj import IrjNumberGenerator
from app.sharepoint import SharePointClient, SharePointError
from app.suppliers import SupplierStore
from app.suppliers import get_supplier as _default_get_supplier
from app.suppliers import list_suppliers as _default_list_suppliers
from app.supplier_terms import SupplierTermsStore
from app.workflow import (
    ConfirmedInvoice,
    RoutingValidationError,
    clean_invoice_filename,
    prefixed_invoice_filename,
    route_confirmed_invoice,
)
from app.statuses import TERMINAL_STATUSES, Status, awaiting_approval, processing_approval


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


class InvoiceLifecycle:
    """Orchestrates every stage of the invoice workflow: AI extraction, company/IRJ/routing, SharePoint filing,
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
        suppliers_store: SupplierStore | None = None,
        supplier_terms_store: SupplierTermsStore | None = None,
        configuration_getter: Callable[[str, str | None], str | None] | None = None,
        extraction_runner: Callable[[Path], ExtractionResult] | None = None,
        classification_runner: Callable[[Path], DocumentClassification] = (
            classify_pdf_document
        ),
    ) -> None:
        self.invoice_store = invoice_store
        self.irj_generator = irj_generator
        self.activity_feed = activity_feed
        self.sharepoint_client = sharepoint_client
        self.companies_store = companies_store
        self.approval_matrix_store = approval_matrix_store
        self.suppliers_store = suppliers_store
        self.supplier_terms_store = supplier_terms_store
        self.configuration_getter = configuration_getter
        # By default extraction is given the configured companies and suppliers
        # so the model can return exactly the names the application knows.
        self.extraction_runner = extraction_runner or self._extract_with_reference_data
        self.classification_runner = classification_runner

    def _extract_with_reference_data(self, pdf_path: Path) -> ExtractionResult:
        return run_ai_extraction(pdf_path, reference=self._reference_data())

    def _reference_data(self) -> ReferenceData | None:
        """Companies and suppliers (with aliases) for the extraction prompt."""
        try:
            companies = (
                self.companies_store.list()
                if self.companies_store is not None
                else _default_list_companies()
            )
            suppliers = (
                self.suppliers_store.list()
                if self.suppliers_store is not None
                else _default_list_suppliers()
            )
        except Exception:
            return None  # extraction still works, just without name hints
        return ReferenceData(
            companies=tuple((c.name, tuple(c.aliases)) for c in companies),
            suppliers=tuple((s.name, tuple(s.aliases)) for s in suppliers),
        )

    def _get_company(self, name: str):
        if self.companies_store is not None:
            return self.companies_store.get(name)
        return _default_get_company(name)

    def _find_approvers(self, company: str, supplier: str):
        if self.approval_matrix_store is not None:
            return self.approval_matrix_store.find(company, supplier)
        return _default_find_approvers(company, supplier)

    def _get_supplier(self, name: str):
        if self.suppliers_store is not None:
            return self.suppliers_store.get(name)
        return _default_get_supplier(name)

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

    def _log_purchase_ledger(
        self, *, event_type: str, message: str, invoice_id: int | None = None
    ) -> None:
        self.activity_feed.add_event(
            event_type=event_type,
            target_role=ROLE_PURCHASE_LEDGER,
            message=message,
            invoice_id=invoice_id,
        )

    @contextmanager
    def _claimed(
        self,
        invoice: InvoiceRecord,
        claim_status: str = Status.PROCESSING_UPDATE,
        *,
        refusal: str | None = None,
    ) -> Iterator[Callable[..., InvoiceRecord]]:
        """Hold ``invoice`` in ``claim_status`` while a stage works on it.

        Only one caller can move an invoice out of its current status, so a
        second action on the same invoice (a double click, two people, the web
        app and the worker) fails cleanly instead of both running. Yields a
        ``save(**fields)`` that commits only while the claim is held. A save
        without ``status`` keeps the claim (an intermediate save); the last
        save must set the status. If anything fails before that, the invoice
        returns to its original status, so nothing is left in a status no
        queue shows."""
        original = invoice.status
        if self.invoice_store.update_fields_if_status(
            invoice.id, original, status=claim_status
        ) is None:
            current = self._require_invoice(invoice.id)
            raise InvoiceLifecycleError(
                f"{refusal or f'Invoice {invoice.id} changed while this action started'} "
                f"(status: {current.status})."
            )

        def save(**fields: object) -> InvoiceRecord:
            record = self.invoice_store.update_fields_if_status(
                invoice.id, claim_status, **fields
            )
            if record is None:
                current = self._require_invoice(invoice.id)
                raise InvoiceLifecycleError(
                    f"Invoice {invoice.id} could not be saved "
                    f"(status: {current.status})."
                )
            return record

        try:
            yield save
        except BaseException:
            # A no-op once save() has run: the claim status is gone by then.
            self.invoice_store.update_fields_if_status(
                invoice.id, claim_status, status=original
            )
            raise

    def _transition(
        self,
        invoice: InvoiceRecord,
        status: str,
        *,
        folder: str | None = None,
        filename: str | None = None,
        move_pdf: bool = True,
        **fields: object,
    ) -> InvoiceRecord:
        """Move ``invoice`` to ``status``: claim it, file its PDF (in
        ``folder``, by default the status's own folder, if it has one), then
        save ``status`` and ``fields``. See _claimed for what happens on
        failure."""
        with self._claimed(invoice) as save:
            if move_pdf:
                destination = folder or self._folder_for_status(invoice, status)
                if destination is not None:
                    self._move_pdf_in_sharepoint(
                        invoice, destination, filename or self._filed_filename(invoice)
                    )
            return save(status=status, **fields)

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

    @staticmethod
    def _as_utc(value: str | datetime | None) -> datetime | None:
        if not value:
            return None
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

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

    # ------------------------------------------------------------------
    # Stage 1: AI extraction
    # ------------------------------------------------------------------

    def run_document_classification(self, invoice_id: int) -> InvoiceRecord:
        document = self._require_invoice(invoice_id)
        if document.status != Status.AWAITING_EXTRACTION:
            raise InvoiceLifecycleError(
                "Document classification may only run before invoice extraction."
            )
        classification = self.classification_runner(Path(document.stored_path))
        if classification.document_type == "statement":
            return self._flag_detected_statement(document, classification)
        return document

    def run_extraction(self, invoice_id: int) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status != Status.AWAITING_EXTRACTION:
            raise InvoiceLifecycleError(
                "AI extraction may only run while an invoice is awaiting extraction."
            )
        # "The PDF cannot be read" is a scenario the system must handle sensibly
        # rather than crash on. Once real AI extraction (Azure Document
        # Intelligence) is wired into run_ai_extraction, a corrupt/unreadable
        # PDF should flag the invoice for review, not raise an unhandled
        # error and stall the pipeline.
        try:
            local_classification = self.classification_runner(
                Path(invoice.stored_path)
            )
            result = (
                None
                if local_classification.document_type == "statement"
                else self.extraction_runner(Path(invoice.stored_path))
            )
        except (
            ExtractionConfigurationError,
            ExtractionServiceError,
        ) as error:
            self._log_purchase_ledger(
                event_type="extraction_unavailable",
                message=(
                    f"Automatic extraction for {invoice.original_filename} "
                    "is temporarily unavailable and will need retrying."
                ),
                invoice_id=invoice_id,
            )
            raise InvoiceExtractionUnavailableError(str(error)) from error
        except Exception as error:
            record = self._save_extraction_outcome(
                invoice,
                status=Status.NEEDS_REVIEW,
                review_return_status=None,
                review_reason=f"The PDF could not be read automatically: {error}",
            )
            self._log_purchase_ledger(
                event_type="needs_review",
                message=(
                    f"Invoice {invoice.original_filename} could not be read "
                    "and needs manual review."
                ),
                invoice_id=invoice_id,
            )
            return record
        if result is None:
            return self._flag_detected_statement(invoice, local_classification)
        if result.document_type == "statement":
            return self._flag_detected_statement(
                invoice,
                DocumentClassification(
                    document_type="statement",
                    confidence=result.document_classification_confidence or 0.0,
                    reason=(
                        result.document_classification_reason
                        or "The PDF was detected as a supplier statement."
                    ),
                ),
            )
        warnings = list(result.warnings)
        company = result.company
        supplier = result.supplier
        company_profile = None
        supplier_profile = None
        if company:
            company_profile = self._get_company(company)
            if company_profile is None:
                warnings.append(
                    f"Company '{company}' does not match a configured company or alias."
                )
            else:
                company = company_profile.name
        if supplier:
            supplier_profile = self._get_supplier(supplier)
            if supplier_profile is None:
                warnings.append(
                    f"Supplier '{supplier}' is not in the supplier register."
                )
            else:
                supplier = supplier_profile.name
        warnings.extend(
            invoice_number_warnings(
                result.supplier_invoice_number,
                supplier=supplier,
                pattern=(
                    supplier_profile.invoice_number_pattern
                    if supplier_profile is not None
                    else None
                ),
            )
        )
        duplicate = None
        if company and supplier and result.supplier_invoice_number:
            duplicate = find_possible_duplicate(
                self.invoice_store,
                exclude_invoice_id=invoice_id,
                company=company,
                supplier=supplier,
                supplier_invoice_number=result.supplier_invoice_number,
            )
            if duplicate is not None:
                reference = duplicate_reference(
                    duplicate.invoice_id, duplicate.irj_number
                )
                warnings.append(
                    f"It's a duplicate of {reference} "
                    f"(status {duplicate.status}), matched on "
                    f"{duplicate.match_basis}."
                )

        invoice_type = "po" if result.purchase_order_number else "nominal"
        extracted_fields = {
            "company": company,
            "supplier": supplier,
            "supplier_invoice_number": result.supplier_invoice_number,
            "po_number": result.purchase_order_number,
            "invoice_date": result.invoice_date,
            "invoice_value": result.invoice_value,
            "currency": result.currency,
        }
        extraction_metadata = {
            "extraction_model": (
                result.model
                or os.environ.get("INVOICE_EXTRACTION_MODEL", "").strip()
                or "unspecified"
            ),
            "extraction_prompt_version": (
                result.prompt_version
                or os.environ.get("INVOICE_EXTRACTION_PROMPT_VERSION", "").strip()
                or "unspecified"
            ),
            "extracted_fields_json": json.dumps(
                extracted_fields, sort_keys=True, separators=(",", ":")
            ),
        }

        if result.needs_review or warnings:
            warning_text = "\n".join(warnings)
            record = self._save_extraction_outcome(
                invoice,
                company=company,
                supplier=supplier,
                supplier_invoice_number=result.supplier_invoice_number,
                po_number=result.purchase_order_number,
                invoice_type=invoice_type,
                invoice_date=result.invoice_date,
                invoice_value=result.invoice_value,
                currency=result.currency,
                ai_confidence=result.confidence,
                ai_field_confidences=result.field_confidences_json(),
                ai_review_warnings=warning_text,
                document_type="invoice",
                document_classification_confidence=(
                    result.document_classification_confidence
                ),
                document_classification_reason=result.document_classification_reason,
                status=Status.NEEDS_REVIEW,
                duplicate_of_invoice_id=(
                    duplicate.invoice_id if duplicate is not None else None
                ),
                review_return_status=None,
                review_reason=warning_text or "Purchase Ledger must confirm invoice details.",
                routing_explanation=(
                    "Automatic routing paused because extraction validation "
                    "produced review warnings."
                ),
                **extraction_metadata,
            )
            self.activity_feed.add_event(
                event_type=(
                    "possible_duplicate" if duplicate is not None else "needs_review"
                ),
                target_role=ROLE_PURCHASE_LEDGER,
                message=(
                    (
                        f"Invoice {invoice.original_filename} looks like a possible "
                        "duplicate of "
                        f"{duplicate_reference(duplicate.invoice_id, duplicate.irj_number)}."
                    )
                    if duplicate is not None
                    else (
                        f"Invoice {invoice.original_filename} needs manual "
                        "confirmation before filing."
                    )
                ),
                invoice_id=invoice_id,
            )
            return record
        # Reached only when extraction is confident: every required field read,
        # no warnings, no duplicate, company and supplier known.
        record = self._save_extraction_outcome(
            invoice,
            company=company,
            supplier=supplier,
            supplier_invoice_number=result.supplier_invoice_number,
            po_number=result.purchase_order_number,
            invoice_type=invoice_type,
            invoice_date=result.invoice_date,
            invoice_value=result.invoice_value,
            currency=result.currency,
            ai_confidence=result.confidence,
            ai_field_confidences=result.field_confidences_json(),
            ai_review_warnings=None,
            document_type="invoice",
            document_classification_confidence=(
                result.document_classification_confidence
            ),
            document_classification_reason=result.document_classification_reason,
            status=Status.NEEDS_REVIEW,
            duplicate_of_invoice_id=None,
            review_return_status=None,
            review_reason="Awaiting Purchase Ledger confirmation.",
            routing_explanation=(
                f"AI suggested the {'PO Matching' if result.purchase_order_number else 'nominal'} "
                "route; Purchase Ledger confirmation is required before filing."
            ),
            **extraction_metadata,
        )
        if self._auto_confirm_enabled():
            return self._auto_confirm(record, result)
        return record

    def _save_extraction_outcome(
        self, invoice: InvoiceRecord, **fields: object
    ) -> InvoiceRecord:
        """Save what extraction found, then move the PDF to Flagged.

        Extraction can take a while; if Purchase Ledger confirmed the invoice
        meanwhile, this stale result must not move its PDF back or overwrite
        the newer status. The save comes first so that a failed move still
        leaves the invoice visible for review (nothing retries an invoice
        left in "Awaiting AI Extraction"); later steps move the PDF by its
        SharePoint item id from wherever it is."""
        record = self.invoice_store.update_fields_if_status(
            invoice.id, Status.AWAITING_EXTRACTION, **fields
        )
        if record is not None:
            self._move_to_flagged(invoice)
            return record
        current = self._require_invoice(invoice.id)
        raise InvoiceLifecycleError(
            f"Invoice {invoice.id} changed while it was being read "
            f"(status: {current.status}); the extraction result was not saved."
        )

    def _auto_confirm_enabled(self) -> bool:
        if self.configuration_getter is None:
            return False
        value = self.configuration_getter("ai_auto_confirm", "false")
        return str(value).strip().casefold() == "true"

    def _auto_confirm(
        self, record: InvoiceRecord, result: ExtractionResult
    ) -> InvoiceRecord:
        """Route a confident invoice on without waiting for Purchase Ledger.

        confirm_and_route repeats the duplicate, invoice-number and approver
        checks. If any of them refuses, the invoice simply stays in Needs
        Review with the reason, exactly as when auto-confirm is off."""
        try:
            return self.confirm_and_route(
                record.id,
                company=record.company or "",
                supplier=record.supplier or "",
                supplier_invoice_number=record.supplier_invoice_number,
                purchase_order_number=record.po_number,
                invoice_date=record.invoice_date,
                invoice_value=record.invoice_value,
                currency=record.currency,
                recorded_by=AUTO_CONFIRM_ACTOR,
            )
        except InvoiceLifecycleError as error:
            held = self.invoice_store.update_fields_if_status(
                record.id,
                Status.NEEDS_REVIEW,
                review_reason=f"Automatic confirmation was not possible: {error}",
            )
            if held is None:
                # Someone else (a manual confirm) moved it on meanwhile.
                return self._require_invoice(record.id)
            self.activity_feed.add_event(
                event_type="needs_review",
                target_role=ROLE_PURCHASE_LEDGER,
                message=(
                    f"Invoice {record.original_filename} could not be confirmed "
                    "automatically and needs manual confirmation."
                ),
                invoice_id=record.id,
            )
            return held

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
    # ------------------------------------------------------------------
    # Manual review flag
    # ------------------------------------------------------------------

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
        company_profile = self._get_company(company)
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

    def route_for_payment(
        self, invoice_id: int, *, route: str, recorded_by: str
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status not in (Status.APPROVED, Status.PAYMENT_ROUTING_ISSUE):
            raise InvoiceLifecycleError(
                f"Only approved invoices can be routed for payment "
                f"(status: {invoice.status})."
            )
        routes = {
            "bacs": (Status.APPROVED_BACS, "BACS"),
            "bankline": (Status.APPROVED_BANKLINE, "Bankline"),
            "foreign_poa": (Status.APPROVED_FOREIGN_POA, "Foreign POA"),
        }
        if route not in routes:
            raise InvoiceLifecycleError(
                "Payment route must be BACS, Bankline, or Foreign POA."
            )
        status, payment_method = routes[route]
        record = self._transition(
            invoice,
            status,
            payment_method=payment_method,
            is_foreign_payment=1 if route == "foreign_poa" else 0,
            payment_route_decided_at=datetime.now(timezone.utc).isoformat(),
            payment_route_decided_by=recorded_by,
            payment_date=None,
            payment_reference=None,
            paid_by=None,
            reconciliation_date=None,
            reconciliation_notes=None,
            reconciled_by=None,
            foreign_allocation_date=None,
            foreign_allocation_reference=None,
            foreign_allocated_by=None,
            hold_reason=None,
        )
        self._log_purchase_ledger(
            event_type="payment_route_selected",
            message=(
                f"Invoice {invoice.irj_number} routed to {payment_method} "
                f"by {recorded_by}."
            ),
            invoice_id=invoice_id,
        )
        return record

    @staticmethod
    def _normalize_payment_route(value: str | None) -> str | None:
        normalized = (value or "").strip().casefold().replace("_", " ").replace("-", " ")
        normalized = " ".join(normalized.split())
        aliases = {
            "bacs": "bacs",
            "bankline": "bankline",
            "foreign poa": "foreign_poa",
            "foreign": "foreign_poa",
        }
        return aliases.get(normalized)

    def auto_route_for_payment(self, invoice_id: int) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status not in (Status.APPROVED, Status.PAYMENT_ROUTING_ISSUE):
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is not ready for payment routing "
                f"(status: {invoice.status})."
            )
        reason: str | None = None
        route: str | None = None
        if self.supplier_terms_store is None:
            reason = "Supplier payment settings are unavailable."
        else:
            profiles = self.supplier_terms_store.list_for_supplier(
                str(invoice.company or ""), str(invoice.supplier or "")
            )
            configured = {
                self._normalize_payment_route(profile.default_payment_method)
                for profile in profiles
                if profile.default_payment_method
            }
            invalid = [
                profile.default_payment_method
                for profile in profiles
                if profile.default_payment_method
                and self._normalize_payment_route(profile.default_payment_method) is None
            ]
            if not profiles or not configured:
                reason = (
                    "No supported default payment method is configured for this "
                    "company and supplier."
                )
            elif invalid:
                reason = f"Unsupported supplier payment method: {invalid[0]}."
            elif len(configured) > 1:
                reason = "Supplier account profiles contain conflicting payment methods."
            else:
                route = next(iter(configured))
        if route is not None:
            return self.route_for_payment(
                invoice_id,
                route=route,
                recorded_by="Automatic supplier payment routing",
            )
        record = self._transition(
            invoice,
            Status.PAYMENT_ROUTING_ISSUE,
            move_pdf=False,
            payment_method=None,
            hold_reason=reason,
            payment_route_decided_at=None,
            payment_route_decided_by=None,
        )
        self._log_purchase_ledger(
            event_type="payment_routing_on_hold",
            message=(
                f"Invoice {invoice.irj_number or invoice.original_filename} is on hold: "
                f"{reason}"
            ),
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
        supplier_account_number: str | None = None,
        payment_method: str | None = None,
        recorded_by: str | None = None,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        payable_statuses = {
            Status.APPROVED_BACS: "BACS",
            Status.APPROVED_BANKLINE: "Bankline",
            Status.APPROVED_FOREIGN_POA: "Foreign POA",
            Status.FOREIGN_AWAITING_ALLOCATION: "Foreign POA",
        }
        if invoice.status not in payable_statuses:
            raise InvoiceLifecycleError(
                f"Only an invoice in a payment section can be marked as paid "
                f"(status: {invoice.status})."
            )
        if not payment_reference or not payment_reference.strip():
            raise InvoiceLifecycleError("A payment reference is required.")
        if not payment_date.strip():
            raise InvoiceLifecycleError("A payment date is required.")
        # Record who marked the invoice as paid.
        record = self._transition(
            invoice,
            Status.PAID,
            payment_date=payment_date,
            supplier_account_number=supplier_account_number,
            payment_reference=payment_reference,
            payment_method=payable_statuses[invoice.status],
            paid_by=recorded_by,
        )
        self._log_purchase_ledger(
            event_type="paid",
            message=f"Invoice {invoice.irj_number} paid; awaiting bank reconciliation.",
            invoice_id=invoice_id,
        )
        return record

    def route_as_foreign_payment(
        self, invoice_id: int, *, recorded_by: str
    ) -> InvoiceRecord:
        return self.route_for_payment(
            invoice_id, route="foreign_poa", recorded_by=recorded_by
        )

    def mark_foreign_allocated(
        self,
        invoice_id: int,
        *,
        allocation_date: str,
        allocation_reference: str,
        recorded_by: str,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status not in {
            Status.FOREIGN_AWAITING_ALLOCATION,
            Status.APPROVED_FOREIGN_POA,
        }:
            raise InvoiceLifecycleError(
                f"Only foreign payments awaiting allocation can be allocated "
                f"(status: {invoice.status})."
            )
        if not allocation_reference.strip():
            raise InvoiceLifecycleError("An allocation reference is required.")
        if not allocation_date.strip():
            raise InvoiceLifecycleError("An allocation date is required.")
        record = self._transition(
            invoice,
            Status.PAID,
            payment_date=allocation_date,
            payment_reference=allocation_reference.strip(),
            payment_method="Foreign POA",
            paid_by=recorded_by,
            foreign_allocation_date=allocation_date,
            foreign_allocation_reference=allocation_reference.strip(),
            foreign_allocated_by=recorded_by,
        )
        self._log_purchase_ledger(
            event_type="foreign_payment_allocated",
            message=(
                f"Foreign payment for invoice {invoice.irj_number} allocated and "
                "is awaiting bank reconciliation."
            ),
            invoice_id=invoice_id,
        )
        return record

    def revert_foreign_payment_route(
        self, invoice_id: int, *, recorded_by: str
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status not in {
            Status.FOREIGN_AWAITING_ALLOCATION,
            Status.APPROVED_FOREIGN_POA,
        }:
            raise InvoiceLifecycleError(
                f"Only a foreign payment awaiting allocation can be returned to "
                f"domestic payment (status: {invoice.status})."
            )
        record = self._transition(
            invoice,
            Status.APPROVED,
            payment_method=None,
            is_foreign_payment=None,
            payment_route_decided_at=None,
            payment_route_decided_by=None,
            foreign_allocation_date=None,
            foreign_allocation_reference=None,
            foreign_allocated_by=None,
        )
        self._log_purchase_ledger(
            event_type="foreign_payment_reverted",
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
        if invoice.status != Status.PAID:
            raise InvoiceLifecycleError(
                f"Only invoices awaiting bank reconciliation can be reconciled "
                f"(status: {invoice.status})."
            )
        # Record who completed the reconciliation, where practical.
        # reconciliation_date is the date Purchase Ledger says the payment
        # appears on the bank statement; reconciled_at is the system
        # timestamp of when the invoice was actually marked reconciled here.
        record = self._transition(
            invoice,
            Status.RECONCILED,
            reconciliation_date=reconciliation_date,
            reconciliation_notes=notes,
            reconciled_by=recorded_by,
            reconciled_at=datetime.now(timezone.utc).isoformat(),
        )
        self._log_purchase_ledger(
            event_type="reconciled",
            message=f"Invoice {invoice.irj_number} reconciled and filed.",
            invoice_id=invoice_id,
        )
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_invoice(self, invoice_id: int) -> InvoiceRecord:
        invoice = self.invoice_store.get(invoice_id)
        if invoice is None:
            raise InvoiceLifecycleError(f"Invoice {invoice_id} was not found.")
        return invoice

    def _flag_detected_statement(
        self,
        document: InvoiceRecord,
        classification: DocumentClassification,
    ) -> InvoiceRecord:
        record = self._save_extraction_outcome(
            document,
            document_type="statement",
            document_classification_confidence=classification.confidence,
            document_classification_reason=classification.reason,
            ai_review_warnings=classification.reason,
            status=Status.NEEDS_REVIEW,
            review_return_status=None,
            review_reason=(
                "Detected as a supplier statement. Select the company and "
                "file it to SharePoint after manual review."
            ),
        )
        self._log_purchase_ledger(
            event_type="statement_detected",
            message=(
                f"'{document.original_filename}' was detected as a supplier "
                "statement and needs manual filing."
            ),
            invoice_id=document.id,
        )
        return record

    def _move_to_flagged(self, invoice: InvoiceRecord) -> None:
        destination = FLAGGED_INVOICES_FOLDER
        if self.sharepoint_client is not None:
            destination = getattr(
                getattr(self.sharepoint_client, "settings", None),
                "flagged_folder",
                destination,
            )
        self._move_pdf_in_sharepoint(
            invoice,
            destination,
            self._filed_filename(invoice),
        )

    def _folder_for_status(
        self, invoice: InvoiceRecord, status: str
    ) -> str | None:
        if status == Status.AWAITING_EXTRACTION:
            if self.sharepoint_client is None:
                return None
            return self.sharepoint_client.settings.incoming_folder
        structure = self._company_folders(invoice)
        if status == Status.AWAITING_SAGE_REGISTRATION:
            return (
                structure.po_match
                if invoice.invoice_type == "po"
                else structure.nominal_invoices
            )
        folder = STATUS_FOLDERS.get(status)
        return getattr(structure, folder) if folder else None

    def _move_pdf_in_sharepoint(
        self,
        invoice: InvoiceRecord,
        destination_folder: str,
        destination_filename: str,
    ) -> None:
        if self.sharepoint_client is None:
            self._log_purchase_ledger(
                event_type="sharepoint_pending",
                message=(
                    "SharePoint is not configured; the PDF was routed but not "
                    "moved. Set SHAREPOINT_SITE_ID / SHAREPOINT_DRIVE_ID to "
                    "enable filing."
                ),
                invoice_id=invoice.id,
            )
            return
        try:
            if invoice.sharepoint_item_id:
                item = self.sharepoint_client.move_to_folder(
                    invoice.sharepoint_item_id,
                    destination_folder,
                    destination_filename,
                )
            else:
                pdf_bytes = Path(invoice.stored_path).read_bytes()
                item = self.sharepoint_client.upload_and_move(
                    filename=invoice.original_filename,
                    content=pdf_bytes,
                    destination_folder=destination_folder,
                    destination_filename=destination_filename,
                )
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise SharePointError(
                    "SharePoint did not return an item ID after moving the invoice."
                )
            self.invoice_store.update_fields(
                invoice.id,
                sharepoint_item_id=item_id,
                sharepoint_web_url=self.sharepoint_client.get_item_web_url(item),
            )
        except SharePointError as error:
            self._log_purchase_ledger(
                event_type="sharepoint_error",
                message=f"SharePoint move failed for invoice {invoice.id}: {error}",
                invoice_id=invoice.id,
            )
            raise InvoiceLifecycleError(
                f"SharePoint filing failed; invoice status was not advanced: {error}"
            ) from error

    def _company_folders(self, invoice: InvoiceRecord) -> CompanyFolderStructure:
        if not invoice.company:
            raise InvoiceLifecycleError(
                f"Invoice {invoice.id} has no company folder configuration."
            )
        company = self._get_company(invoice.company)
        if company is None:
            raise InvoiceLifecycleError(
                f"Company '{invoice.company}' is not configured."
            )
        return CompanyFolderStructure.from_root(company.sharepoint_root_folder)

    @staticmethod
    def _filed_filename(invoice: InvoiceRecord) -> str:
        if not invoice.irj_number:
            return clean_invoice_filename(invoice.original_filename)
        return prefixed_invoice_filename(
            invoice.irj_number, invoice.original_filename
        )

    @classmethod
    def _rejected_filename(cls, invoice: InvoiceRecord) -> str:
        if invoice.irj_number:
            return cls._filed_filename(invoice)
        prefix = f"invoice-{invoice.id}_"
        original_filename = clean_invoice_filename(invoice.original_filename)
        return (
            original_filename
            if original_filename.startswith(prefix)
            else f"{prefix}{original_filename}"
        )
