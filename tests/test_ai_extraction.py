from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest

from app.activity_feed import ActivityFeedStore
from app.ai_extraction import (
    AzureInvoiceExtractor,
    DocumentIntelligenceConfigurationError,
    DocumentIntelligenceSettings,
)
from app.companies import CompanyStore
from app.config_db import set_setting
from app.invoice_lifecycle import InvoiceLifecycle
from app.invoices import InvoiceStore
from app.irj import IrjNumberGenerator
from app.suppliers import SupplierStore


@dataclass
class CurrencyValue:
    amount: float
    currency_code: str


class Field:
    def __init__(
        self,
        value: object,
        confidence: float = 0.95,
        *,
        attribute: str = "value_string",
    ) -> None:
        setattr(self, attribute, value)
        self.confidence = confidence


class Poller:
    def __init__(self, fields: dict[str, Field], content: str = "") -> None:
        self.fields = fields
        self.content = content

    def result(self) -> object:
        return SimpleNamespace(
            documents=[SimpleNamespace(fields=self.fields)],
            content=self.content,
        )


class FakeClient:
    def __init__(self, fields: dict[str, Field], content: str = "") -> None:
        self.fields = fields
        self.content = content
        self.calls: list[tuple[str, bytes]] = []

    def begin_analyze_document(self, model_id: str, *, body: BinaryIO) -> Poller:
        self.calls.append((model_id, body.read()))
        return Poller(self.fields, self.content)


def complete_fields() -> dict[str, Field]:
    return {
        "CustomerName": Field("Acme Trading Ltd"),
        "VendorName": Field("Supplier Ltd"),
        "InvoiceId": Field("INV-1001"),
        "PurchaseOrder": Field("PO-500"),
        "InvoiceDate": Field(date(2026, 9, 8), attribute="value_date"),
        "InvoiceTotal": Field(
            CurrencyValue(1250.75, "GBP"),
            attribute="value_currency",
        ),
    }


def test_maps_prebuilt_invoice_fields_and_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONFIG_DB_PATH", str(tmp_path / "config.db"))
    monkeypatch.setenv("CONFIG_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AI_CONFIDENCE_THRESHOLD", "0.80")
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\ninvoice\n%%EOF")
    client = FakeClient(complete_fields())
    extractor = AzureInvoiceExtractor(
        DocumentIntelligenceSettings("https://documents.example.test/"),
        client=client,
    )

    result = extractor.extract(pdf)

    assert result.company == "Acme Trading Ltd"
    assert result.supplier == "Supplier Ltd"
    assert result.supplier_invoice_number == "INV-1001"
    assert result.purchase_order_number == "PO-500"
    assert result.invoice_date == "2026-09-08"
    assert result.invoice_value == 1250.75
    assert result.currency == "GBP"
    assert result.confidence == pytest.approx(0.95)
    assert result.needs_review is False
    assert result.warnings == ()
    assert client.calls == [("prebuilt-invoice", pdf.read_bytes())]


def test_missing_or_uncertain_critical_fields_require_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONFIG_DB_PATH", str(tmp_path / "config.db"))
    monkeypatch.setenv("CONFIG_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AI_CONFIDENCE_THRESHOLD", "0.80")
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\ninvoice\n%%EOF")
    fields = complete_fields()
    fields.pop("InvoiceId")
    fields["VendorName"].confidence = 0.60

    result = AzureInvoiceExtractor(
        DocumentIntelligenceSettings("https://documents.example.test/"),
        client=FakeClient(fields),
    ).extract(pdf)

    assert result.needs_review is True
    assert result.confidence == 0.0
    assert any("supplier invoice number" in warning for warning in result.warnings)
    assert any(
        "'supplier' confidence" in warning and "60%" in warning
        for warning in result.warnings
    )


def test_recovers_untyped_invoice_total_and_currency_symbol(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\ninvoice\n%%EOF")
    fields = complete_fields()
    fields["InvoiceTotal"] = Field(
        "Invoice Total: £1,250.75",
        confidence=0.91,
    )

    result = AzureInvoiceExtractor(
        DocumentIntelligenceSettings("https://documents.example.test/"),
        client=FakeClient(fields),
    ).extract(pdf)

    assert result.invoice_value == 1250.75
    assert result.currency == "GBP"
    assert not any("invoice value' is missing" in warning for warning in result.warnings)
    assert not any("currency' is missing" in warning for warning in result.warnings)


def test_recovers_labelled_invoice_fields_from_ocr_for_manual_review(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\ninvoice\n%%EOF")
    fields = complete_fields()
    fields.pop("InvoiceId")
    fields.pop("InvoiceTotal")

    result = AzureInvoiceExtractor(
        DocumentIntelligenceSettings("https://documents.example.test/"),
        client=FakeClient(
            fields,
            content="Invoice No. INV-2048\nInvoice Total: GBP 2,450.60",
        ),
    ).extract(pdf)

    assert result.supplier_invoice_number == "INV-2048"
    assert result.invoice_value == 2450.60
    assert result.currency == "GBP"
    assert result.needs_review is True
    assert any("recovered from OCR" in warning for warning in result.warnings)


def test_uses_amount_due_as_flagged_total_fallback(tmp_path: Path) -> None:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\ninvoice\n%%EOF")
    fields = complete_fields()
    fields.pop("InvoiceTotal")
    fields["AmountDue"] = Field(
        CurrencyValue(99.50, "GBP"),
        confidence=0.97,
        attribute="value_currency",
    )

    result = AzureInvoiceExtractor(
        DocumentIntelligenceSettings("https://documents.example.test/"),
        client=FakeClient(fields),
    ).extract(pdf)

    assert result.invoice_value == 99.50
    assert result.currency == "GBP"
    assert result.needs_review is True
    assert any("amount-due field" in warning for warning in result.warnings)


def test_ocr_content_detects_supplier_statement_before_invoice_mapping(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(b"%PDF-1.7\nstatement\n%%EOF")
    client = FakeClient(
        {},
        content=(
            "STATEMENT OF ACCOUNT Opening balance "
            "Invoice date Invoice number Debit Credit Closing balance"
        ),
    )

    result = AzureInvoiceExtractor(
        DocumentIntelligenceSettings("https://documents.example.test/"),
        client=client,
    ).extract(pdf)

    assert result.document_type == "statement"
    assert result.needs_review is True
    assert result.document_classification_confidence is not None
    assert result.document_classification_confidence >= 0.80


def test_uncertain_present_po_number_requires_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONFIG_DB_PATH", str(tmp_path / "config.db"))
    monkeypatch.setenv("CONFIG_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AI_CONFIDENCE_THRESHOLD", "0.80")
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\ninvoice\n%%EOF")
    fields = complete_fields()
    fields["PurchaseOrder"].confidence = 0.40

    result = AzureInvoiceExtractor(
        DocumentIntelligenceSettings("https://documents.example.test/"),
        client=FakeClient(fields),
    ).extract(pdf)

    assert result.needs_review is True
    assert any("purchase order number" in warning for warning in result.warnings)


def test_admin_threshold_overrides_environment_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.db"
    monkeypatch.setenv("CONFIG_DB_PATH", str(config_path))
    monkeypatch.setenv("CONFIG_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AI_CONFIDENCE_THRESHOLD", "0.90")
    set_setting("ai_confidence_threshold", "0.70", database_path=config_path)
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\ninvoice\n%%EOF")
    fields = complete_fields()
    for field in fields.values():
        field.confidence = 0.80

    result = AzureInvoiceExtractor(
        DocumentIntelligenceSettings("https://documents.example.test/"),
        client=FakeClient(fields),
    ).extract(pdf)

    assert result.needs_review is False


def test_lifecycle_persists_partial_extraction_and_warnings(tmp_path: Path) -> None:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\ninvoice\n%%EOF")
    invoices = InvoiceStore(tmp_path / "invoices.db")
    record = invoices.add_from_outlook(
        message={"id": "message-1"},
        attachment={"id": "attachment-1", "name": "invoice.pdf", "size": 10},
        stored_path=pdf,
    )
    fields = complete_fields()
    fields.pop("InvoiceId")
    extractor = AzureInvoiceExtractor(
        DocumentIntelligenceSettings("https://documents.example.test/"),
        client=FakeClient(fields),
    )
    lifecycle = InvoiceLifecycle(
        invoices,
        IrjNumberGenerator(tmp_path / "invoices.db"),
        ActivityFeedStore(tmp_path / "activity.db"),
        None,
        extraction_runner=extractor.extract,
    )

    extracted = lifecycle.run_extraction(record.id)

    assert extracted.status == "Needs Review"
    assert extracted.company == "Acme Trading Ltd"
    assert extracted.supplier == "Supplier Ltd"
    assert extracted.supplier_invoice_number is None
    assert extracted.ai_confidence == 0.0
    assert "supplier invoice number" in str(extracted.ai_review_warnings)


@pytest.mark.parametrize(
    ("has_po_number", "expected_type"),
    [
        (False, "nominal"),
        (True, "po"),
    ],
)
def test_flagged_extraction_moves_to_shared_flagged_folder(
    tmp_path: Path,
    has_po_number: bool,
    expected_type: str,
) -> None:
    class FakeSharePointClient:
        def __init__(self) -> None:
            self.moves: list[tuple[str, str, str]] = []

        def move_to_folder(
            self, item_id: str, folder: str, filename: str
        ) -> dict[str, object]:
            self.moves.append((item_id, folder, filename))
            return {
                "id": item_id,
                "name": filename,
                "webUrl": f"https://sharepoint.example/{folder}/{filename}",
            }

        @staticmethod
        def get_item_web_url(item: dict[str, object]) -> str:
            return str(item["webUrl"])

    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\ninvoice\n%%EOF")
    invoices = InvoiceStore(tmp_path / "invoices.db")
    record = invoices.add_from_outlook(
        message={"id": "message-1"},
        attachment={"id": "attachment-1", "name": "invoice.pdf"},
        stored_path=pdf,
    )
    invoices.update_fields(record.id, sharepoint_item_id="drive-item-1")
    fields = complete_fields()
    if not has_po_number:
        fields.pop("PurchaseOrder")
    extractor = AzureInvoiceExtractor(
        DocumentIntelligenceSettings("https://documents.example.test/"),
        client=FakeClient(fields),
    )
    sharepoint = FakeSharePointClient()
    lifecycle = InvoiceLifecycle(
        invoices,
        IrjNumberGenerator(tmp_path / "invoices.db"),
        ActivityFeedStore(tmp_path / "activity.db"),
        sharepoint,  # type: ignore[arg-type]
        companies_store=CompanyStore(tmp_path / "config.db"),
        suppliers_store=SupplierStore(tmp_path / "config.db"),
        extraction_runner=extractor.extract,
    )

    extracted = lifecycle.run_extraction(record.id)

    assert extracted.status == "Needs Review"
    assert extracted.invoice_type == expected_type
    assert sharepoint.moves == [
        ("drive-item-1", "Invoices/Flagged Invoices", "invoice.pdf")
    ]


def test_extraction_immediately_flags_matching_supplier_invoice_number(
    tmp_path: Path,
) -> None:
    first_pdf = tmp_path / "original.pdf"
    first_pdf.write_bytes(b"%PDF-1.7\noriginal invoice\n%%EOF")
    second_pdf = tmp_path / "rescanned.pdf"
    second_pdf.write_bytes(b"%PDF-1.7\nrescanned invoice\n%%EOF")
    invoices = InvoiceStore(tmp_path / "invoices.db")
    original = invoices.add_from_outlook(
        message={"id": "message-1"},
        attachment={"id": "attachment-1", "name": "original.pdf"},
        stored_path=first_pdf,
    )
    invoices.update_fields(
        original.id,
        company="Acme Trading Ltd",
        supplier="Supplier Ltd",
        supplier_invoice_number="INV-1001",
        status="Awaiting Sage Registration",
    )
    resent = invoices.add_from_outlook(
        message={"id": "message-2"},
        attachment={"id": "attachment-2", "name": "rescanned.pdf"},
        stored_path=second_pdf,
    )
    extractor = AzureInvoiceExtractor(
        DocumentIntelligenceSettings("https://documents.example.test/"),
        client=FakeClient(complete_fields()),
    )
    lifecycle = InvoiceLifecycle(
        invoices,
        IrjNumberGenerator(tmp_path / "invoices.db"),
        ActivityFeedStore(tmp_path / "activity.db"),
        None,
        extraction_runner=extractor.extract,
    )

    duplicate = lifecycle.run_extraction(resent.id)

    assert duplicate.status == "Needs Review"
    assert duplicate.duplicate_of_invoice_id == original.id
    assert "matched on supplier invoice number" in str(duplicate.review_reason)


def test_document_intelligence_requires_https_endpoint() -> None:
    with pytest.raises(DocumentIntelligenceConfigurationError, match="HTTPS"):
        AzureInvoiceExtractor(DocumentIntelligenceSettings("http://example.test"))


def test_extraction_cannot_overwrite_a_confirmed_invoice(tmp_path: Path) -> None:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\ninvoice\n%%EOF")
    invoices = InvoiceStore(tmp_path / "invoices.db")
    record = invoices.add_from_outlook(
        message={"id": "message-1"},
        attachment={"id": "attachment-1", "name": "invoice.pdf", "size": 10},
        stored_path=pdf,
    )
    invoices.update_fields(
        record.id,
        status="Approved",
        supplier_invoice_number="HUMAN-CONFIRMED",
    )
    lifecycle = InvoiceLifecycle(
        invoices,
        IrjNumberGenerator(tmp_path / "invoices.db"),
        ActivityFeedStore(tmp_path / "activity.db"),
        None,
        extraction_runner=lambda path: AzureInvoiceExtractor(
            DocumentIntelligenceSettings("https://documents.example.test/"),
            client=FakeClient(complete_fields()),
        ).extract(path),
    )

    with pytest.raises(ValueError, match="awaiting extraction"):
        lifecycle.run_extraction(record.id)

    assert invoices.get(record.id).supplier_invoice_number == "HUMAN-CONFIRMED"
