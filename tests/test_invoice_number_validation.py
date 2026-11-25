from pathlib import Path

import pytest

from app.approval_matrix import ApprovalMatrixStore
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
        approval_matrix_store=ApprovalMatrixStore(tmp_path / "config.db"),
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


@pytest.mark.parametrize(
    ("value", "sources"),
    [
        # Today's real invoices: the number read is in the file name.
        ("335835", ("335835.pdf", None)),
        ("2813266", ("15462462_2813266_1788162991.pdf", None)),
        ("INV-21747", ("Invoice INV-21747.pdf", None)),
        ("SI00055502", ("SalesInvoice_SI00055502.pdf", None)),
        # Nothing similar in the file name or subject: nothing to say.
        ("2930", ("Invoice_from_Tokolosh_Ltd.pdf", "Your invoice")),
        ("123", ("124.pdf", None)),  # too short to judge
        (None, ("335835.pdf", None)),
    ],
)
def test_no_near_miss_warning(value: str | None, sources: tuple[str | None, ...]) -> None:
    from app.invoice_number_validation import near_miss_number_warnings

    assert near_miss_number_warnings(value, *sources) == []


@pytest.mark.parametrize(
    ("value", "sources", "suggested"),
    [
        ("335235", ("335835.pdf", None), "335835"),  # a misread seen live
        ("335826", ("335828.pdf", None), "335828"),  # another one
        ("INV-1001", ("scan.pdf", "Invoice INV-1007 attached"), "INV-1007"),
    ],
)
def test_near_miss_warning(
    value: str, sources: tuple[str | None, ...], suggested: str
) -> None:
    from app.invoice_number_validation import near_miss_number_warnings

    [warning] = near_miss_number_warnings(value, *sources)
    assert f"suggests '{suggested}'" in warning
