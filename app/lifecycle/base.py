"""State shared by every stage: the stores, the claim-and-transition helpers and SharePoint filing."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Callable, Iterator

from app.activity_feed import ROLE_PURCHASE_LEDGER, ActivityFeedStore
from app.ai_extraction import ExtractionResult, ReferenceData, run_ai_extraction
from app.approval_matrix import ApprovalMatrixStore
from app.approval_matrix import find_approvers as _default_find_approvers
from app.companies import CompanyStore
from app.companies import get_company as _default_get_company
from app.companies import list_companies as _default_list_companies
from app.company_folders import FLAGGED_INVOICES_FOLDER, CompanyFolderStructure
from app.document_classification import (
    DocumentClassification,
    classify_pdf_document,
)
from app.invoices import InvoiceRecord, InvoiceStore
from app.irj import IrjNumberGenerator
from app.sharepoint import SharePointClient, SharePointError
from app.suppliers import SupplierStore
from app.suppliers import get_supplier as _default_get_supplier
from app.suppliers import list_suppliers as _default_list_suppliers
from app.supplier_terms import SupplierTermsStore
from app.workflow import clean_invoice_filename, prefixed_invoice_filename
from app.statuses import Status
from app.lifecycle.common import STATUS_FOLDERS, InvoiceLifecycleError


class LifecycleBase:
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
    def _as_utc(value: str | datetime | None) -> datetime | None:
        if not value:
            return None
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

    def _require_invoice(self, invoice_id: int) -> InvoiceRecord:
        invoice = self.invoice_store.get(invoice_id)
        if invoice is None:
            raise InvoiceLifecycleError(f"Invoice {invoice_id} was not found.")
        return invoice

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
