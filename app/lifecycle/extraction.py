"""Stage 1: document classification and AI extraction of a newly registered PDF."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.activity_feed import ROLE_PURCHASE_LEDGER
from app.ai_extraction import (
    ExtractionConfigurationError,
    ExtractionResult,
    ExtractionServiceError,
)
from app.document_classification import DocumentClassification
from app.duplicates import duplicate_reference, find_possible_duplicate
from app.invoices import InvoiceRecord
from app.invoice_number_validation import invoice_number_warnings
from app.statuses import Status
from app.lifecycle.common import (
    AUTO_CONFIRM_ACTOR,
    InvoiceExtractionUnavailableError,
    InvoiceLifecycleError,
)
from app.lifecycle.base import LifecycleBase

logger = logging.getLogger("invoice_processor")


class ExtractionStage(LifecycleBase):
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
            company_profile = self.companies_store.get(company)
            if company_profile is None:
                warnings.append(
                    f"Company '{company}' does not match a configured company or alias."
                )
            else:
                company = company_profile.name
        if supplier:
            supplier_profile = self.suppliers_store.get(supplier)
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
        checks. If any of them refuses, or routing fails unexpectedly (a
        database or SharePoint error), the invoice simply stays in Needs
        Review with the reason, exactly as when auto-confirm is off: the
        upload or email that brought it in still succeeds."""
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
        except Exception as error:
            if not isinstance(error, InvoiceLifecycleError):
                logger.exception("Automatic confirmation of invoice %s failed.", record.id)
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
