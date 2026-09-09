from pathlib import Path

import pytest

from app.activity_feed import ActivityFeedStore
from app.ai_extraction import ExtractionResult
from app.companies import CompanyStore
from app.invoice_lifecycle import InvoiceLifecycle, InvoiceLifecycleError
from app.invoice_number_validation import (
    invoice_number_warnings,
    validate_invoice_number_pattern,
)
from app.invoices import InvoiceStore
from app.irj import IrjNumberGenerator
from app.suppliers import SupplierStore
from tests.pdf_helpers import VALID_PDF_BYTES


def test_alphanumeric_invoice_numbers_are_valid() -> None:
    assert invoice_number_warnings("INV-1001/A") == []
    assert invoice_number_warnings(
        "21116119LNN",
        supplier="Aramex",
        pattern="########@@@",
    ) == []


def test_disconnected_azure_components_require_review() -> None:
    warnings = invoice_number_warnings(
        "LNN\n21116119",
        supplier="Aramex",
        pattern="########@@@",
    )

    assert any("whitespace or disconnected" in warning for warning in warnings)
    assert any("configured pattern" in warning for warning in warnings)


def test_supplier_pattern_mismatch_requires_review() -> None:
    warnings = invoice_number_warnings(
        "LNN21116119",
        supplier="Aramex",
        pattern="########@@@",
    )

    assert warnings == [
        "Supplier invoice number does not match the configured pattern "
        "for 'Aramex' (########@@@)."
    ]


def test_pattern_requires_safe_placeholders() -> None:
    with pytest.raises(ValueError, match="must contain"):
        validate_invoice_number_pattern("INVOICE")


def _lifecycle_with_pattern(tmp_path: Path, invoice_number: str):
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(VALID_PDF_BYTES)
    invoices = InvoiceStore(tmp_path / "invoices.db")
    record = invoices.add_from_outlook(
        message={"id": "message-1"},
        attachment={"id": "attachment-1", "name": "invoice.pdf"},
        stored_path=pdf,
    )
    suppliers = SupplierStore(tmp_path / "config.db")
    suppliers.create(
        name="Aramex",
        invoice_number_pattern="########@@@",
    )
    lifecycle = InvoiceLifecycle(
        invoices,
        IrjNumberGenerator(tmp_path / "invoices.db"),
        ActivityFeedStore(tmp_path / "activity.db"),
        None,
        companies_store=CompanyStore(tmp_path / "config.db"),
        suppliers_store=suppliers,
        extraction_runner=lambda path: ExtractionResult(
            company=None,
            supplier="Aramex",
            supplier_invoice_number=invoice_number,
            purchase_order_number=None,
            invoice_date="2026-08-31",
            invoice_value=100.0,
            currency="GBP",
            confidence=0.90,
            needs_review=False,
            field_confidences={"supplier_invoice_number": 0.90},
            warnings=(),
        ),
    )
    return lifecycle, record


def test_extraction_flags_suspicious_supplier_invoice_number(
    tmp_path: Path,
) -> None:
    lifecycle, record = _lifecycle_with_pattern(tmp_path, "LNN\n21116119")

    extracted = lifecycle.run_extraction(record.id)

    assert extracted.status == "Needs Review"
    assert "disconnected components" in str(extracted.ai_review_warnings)
    assert "configured pattern" in str(extracted.ai_review_warnings)


def test_confirmation_rejects_uncorrected_supplier_invoice_number(
    tmp_path: Path,
) -> None:
    lifecycle, record = _lifecycle_with_pattern(tmp_path, "LNN\n21116119")

    with pytest.raises(InvoiceLifecycleError, match="disconnected components"):
        lifecycle.confirm_and_route(
            record.id,
            company="Acme Trading Ltd",
            supplier="Aramex",
            supplier_invoice_number="LNN\n21116119",
            purchase_order_number=None,
            invoice_date="2026-08-31",
            invoice_value=100.0,
            currency="GBP",
        )
