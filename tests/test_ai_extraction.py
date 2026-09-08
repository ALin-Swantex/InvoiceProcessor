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
from app.config_db import set_setting
from app.invoice_lifecycle import InvoiceLifecycle
from app.invoices import InvoiceStore
from app.irj import IrjNumberGenerator


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
    def __init__(self, fields: dict[str, Field]) -> None:
        self.fields = fields

    def result(self) -> object:
        return SimpleNamespace(
            documents=[SimpleNamespace(fields=self.fields)]
        )


class FakeClient:
    def __init__(self, fields: dict[str, Field]) -> None:
        self.fields = fields
        self.calls: list[tuple[str, bytes]] = []

    def begin_analyze_document(self, model_id: str, *, body: BinaryIO) -> Poller:
        self.calls.append((model_id, body.read()))
        return Poller(self.fields)


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


def test_uncertain_present_po_number_requires_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONFIG_DB_PATH", str(tmp_path / "config.db"))
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
