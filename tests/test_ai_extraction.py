from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfReader, PdfWriter

from app.activity_feed import ActivityFeedStore
from app.ai_extraction import (
    CERTAINTY_SCORES,
    OUTPUT_SCHEMA,
    PROMPT_VERSION,
    ClaudeInvoiceExtractor,
    ExtractionConfigurationError,
    ExtractionError,
    ExtractionServiceError,
    ExtractionSettings,
    ReferenceData,
    ai_extraction_configured,
    run_ai_extraction,
)
from app.companies import CompanyStore
from app.config_db import set_setting
from app.demo import sample_pdf
from app.invoice_lifecycle import InvoiceLifecycle
from app.invoices import InvoiceStore
from app.irj import IrjNumberGenerator
from app.suppliers import SupplierStore


# --- fakes ----------------------------------------------------------------


def field(
    value: object,
    certainty: str = "high",
    evidence: str | None = "printed on the invoice",
) -> dict[str, object]:
    return {"value": value, "evidence": evidence, "page": 1, "certainty": certainty}


def llm_data(**overrides: Any) -> dict[str, Any]:
    """A complete, confident model answer; override any field by name."""
    data: dict[str, Any] = {
        "document_type": "invoice",
        "document_type_certainty": "high",
        "document_type_reason": "Has an invoice number and a total.",
        "company": field("Acme Trading Ltd"),
        "supplier": field("Supplier Ltd"),
        "supplier_invoice_number": field("INV-1001"),
        "purchase_order_number": field("PO-500"),
        "invoice_date": field("2026-09-08"),
        "net_amount": field(None, "low", None),
        "tax_amount": field(None, "low", None),
        "invoice_value": field(1250.75),
        "currency": field("GBP"),
        "notes": [],
    }
    data.update(overrides)
    return data


class FakeMessages:
    def __init__(self, data: dict[str, Any] | Exception, **response: Any) -> None:
        self.data = data
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if isinstance(self.data, Exception):
            raise self.data
        return SimpleNamespace(
            stop_reason=self.response.get("stop_reason", "end_turn"),
            content=[
                SimpleNamespace(
                    type="text",
                    text=self.response.get("text", json.dumps(self.data)),
                )
            ],
        )


class FakeClient:
    def __init__(self, data: dict[str, Any] | Exception, **response: Any) -> None:
        self.messages = FakeMessages(data, **response)


def extractor_for(
    data: dict[str, Any] | Exception, **response: Any
) -> tuple[ClaudeInvoiceExtractor, FakeClient]:
    client = FakeClient(data, **response)
    return ClaudeInvoiceExtractor(ExtractionSettings(), client=client), client


def write_pdf(path: Path, content: bytes = b"%PDF-1.7\ninvoice\n%%EOF") -> Path:
    path.write_bytes(content)
    return path


# --- request shape ---------------------------------------------------------


def test_request_carries_the_pdf_schema_and_known_names(tmp_path: Path) -> None:
    extractor, client = extractor_for(llm_data())
    pdf = write_pdf(tmp_path / "invoice.pdf")
    reference = ReferenceData(
        companies=(("Acme Trading Ltd", ("Acme",)),),
        suppliers=(("Supplier Ltd", ()),),
    )

    extractor.extract(pdf, reference=reference)

    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["effort"] == "medium"
    assert call["output_config"]["format"] == {
        "type": "json_schema",
        "schema": OUTPUT_SCHEMA,
    }
    document = call["messages"][0]["content"][0]
    assert document["type"] == "document"
    assert document["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(document["source"]["data"]) == pdf.read_bytes()
    names = call["system"][1]
    assert "Acme Trading Ltd (also written: Acme)" in names["text"]
    assert "Supplier Ltd" in names["text"]
    assert names["cache_control"] == {"type": "ephemeral"}


def test_blank_effort_leaves_the_parameter_out(tmp_path: Path) -> None:
    client = FakeClient(llm_data())
    ClaudeInvoiceExtractor(ExtractionSettings(effort=""), client=client).extract(
        write_pdf(tmp_path / "invoice.pdf")
    )

    assert "effort" not in client.messages.calls[0]["output_config"]


def test_long_invoice_sends_only_first_and_last_pages(tmp_path: Path) -> None:
    output = BytesIO()
    writer = PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=200, height=200)
    writer.write(output)
    extractor, client = extractor_for(llm_data())

    extractor.extract(write_pdf(tmp_path / "long.pdf", output.getvalue()))

    sent = base64.b64decode(
        client.messages.calls[0]["messages"][0]["content"][0]["source"]["data"]
    )
    assert len(PdfReader(BytesIO(sent)).pages) == 2


# --- mapping and confidence --------------------------------------------------


def test_confident_complete_extraction_needs_no_review(tmp_path: Path) -> None:
    extractor, _ = extractor_for(llm_data())

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert result.needs_review is False
    assert result.warnings == ()
    assert result.company == "Acme Trading Ltd"
    assert result.supplier == "Supplier Ltd"
    assert result.supplier_invoice_number == "INV-1001"
    assert result.purchase_order_number == "PO-500"
    assert result.invoice_date == "2026-09-08"
    assert result.invoice_value == 1250.75
    assert result.currency == "GBP"
    assert result.confidence == CERTAINTY_SCORES["high"]
    assert result.model == "claude-opus-5-5"
    assert result.prompt_version == PROMPT_VERSION


def test_missing_required_field_requires_review(tmp_path: Path) -> None:
    extractor, _ = extractor_for(
        llm_data(supplier_invoice_number=field(None, "low", None))
    )

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert result.needs_review is True
    assert result.supplier_invoice_number is None
    assert result.confidence == 0.0
    assert "supplier invoice number" in " ".join(result.warnings)


def test_medium_certainty_falls_below_the_default_threshold(tmp_path: Path) -> None:
    extractor, _ = extractor_for(llm_data(invoice_value=field(1250.75, "medium")))

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert result.needs_review is True
    assert result.field_confidences["invoice_value"] == CERTAINTY_SCORES["medium"]
    assert any("invoice value" in warning for warning in result.warnings)


def test_value_without_evidence_is_never_high_confidence(tmp_path: Path) -> None:
    extractor, _ = extractor_for(llm_data(currency=field("GBP", "high", None)))

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert result.needs_review is True
    assert result.field_confidences["currency"] < 0.80


def test_uncertain_present_po_number_requires_review(tmp_path: Path) -> None:
    extractor, _ = extractor_for(llm_data(purchase_order_number=field("PO-9", "low")))

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert result.needs_review is True
    assert any("purchase order number" in warning for warning in result.warnings)


def test_missing_po_number_is_not_a_warning(tmp_path: Path) -> None:
    extractor, _ = extractor_for(
        llm_data(purchase_order_number=field(None, "low", None))
    )

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert result.needs_review is False
    assert result.purchase_order_number is None


def test_admin_threshold_overrides_environment_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.db"
    monkeypatch.setenv("CONFIG_DB_PATH", str(config_path))
    monkeypatch.setenv("CONFIG_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AI_CONFIDENCE_THRESHOLD", "0.90")
    set_setting("ai_confidence_threshold", "0.70", database_path=config_path)
    medium = {
        name: field(value, "medium")
        for name, value in {
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": "INV-1001",
            "invoice_date": "2026-09-08",
            "invoice_value": 1250.75,
            "currency": "GBP",
        }.items()
    }
    extractor, _ = extractor_for(llm_data(**medium))

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert result.needs_review is False


def test_supplier_statement_is_reported_as_a_statement(tmp_path: Path) -> None:
    extractor, _ = extractor_for(
        llm_data(
            document_type="statement",
            document_type_reason="Statement of account with a list of invoices.",
        )
    )

    result = extractor.extract(write_pdf(tmp_path / "statement.pdf"))

    assert result.document_type == "statement"
    assert result.needs_review is True
    assert result.invoice_value is None
    assert result.document_classification_confidence == CERTAINTY_SCORES["high"]
    assert "Statement of account" in (result.document_classification_reason or "")


def test_credit_note_and_model_notes_become_warnings(tmp_path: Path) -> None:
    extractor, _ = extractor_for(
        llm_data(
            document_type="credit_note",
            document_type_reason="Titled Credit Note.",
            notes=["Handwritten amount on page 2"],
        )
    )

    result = extractor.extract(write_pdf(tmp_path / "credit.pdf"))

    assert result.needs_review is True
    joined = " ".join(result.warnings)
    assert "credit note" in joined
    assert "Handwritten amount on page 2" in joined


def test_unreadable_date_is_dropped_with_a_warning(tmp_path: Path) -> None:
    extractor, _ = extractor_for(llm_data(invoice_date=field("8th Sept", "high")))

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert result.invoice_date is None
    assert result.needs_review is True
    assert any("not a valid date" in warning for warning in result.warnings)


# --- deterministic checks ----------------------------------------------------


def text_pdf(tmp_path: Path) -> Path:
    return write_pdf(
        tmp_path / "text.pdf",
        sample_pdf(
            "Supplier Ltd tax invoice",
            ["Invoice no. INV-1001", "PO PO-500", "Total payable 1,250.75 GBP"],
        ),
    )


def test_values_found_in_the_document_text_pass(tmp_path: Path) -> None:
    extractor, _ = extractor_for(llm_data())

    result = extractor.extract(text_pdf(tmp_path))

    assert result.needs_review is False


def test_values_missing_from_the_document_text_are_flagged(tmp_path: Path) -> None:
    extractor, _ = extractor_for(
        llm_data(
            supplier_invoice_number=field("INV-9999"),
            invoice_value=field(5000.00),
        )
    )

    result = extractor.extract(text_pdf(tmp_path))

    joined = " ".join(result.warnings)
    assert "INV-9999" in joined and "not found in the document text" in joined
    assert "5000.00" in joined
    assert result.field_confidences["supplier_invoice_number"] <= 0.30
    assert result.needs_review is True


def test_amounts_that_do_not_add_up_are_flagged(tmp_path: Path) -> None:
    extractor, _ = extractor_for(
        llm_data(net_amount=field(1000.00), tax_amount=field(100.00))
    )

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert any("does not equal the total" in warning for warning in result.warnings)
    assert result.field_confidences["invoice_value"] <= 0.30


def test_amounts_that_add_up_pass(tmp_path: Path) -> None:
    extractor, _ = extractor_for(
        llm_data(
            net_amount=field(1042.29),
            tax_amount=field(208.46),
            invoice_value=field(1250.75),
        )
    )

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert result.needs_review is False


def test_company_must_be_a_configured_company(tmp_path: Path) -> None:
    reference = ReferenceData(companies=(("Acme Trading Ltd", ("Acme",)),))
    extractor, _ = extractor_for(llm_data(company=field("Somebody Else Ltd")))

    result = extractor.extract(
        write_pdf(tmp_path / "invoice.pdf"), reference=reference
    )

    assert any("not one of the configured companies" in w for w in result.warnings)
    assert result.needs_review is True


def test_known_company_alias_is_accepted(tmp_path: Path) -> None:
    reference = ReferenceData(companies=(("Acme Trading Ltd", ("Acme",)),))
    extractor, _ = extractor_for(llm_data(company=field("Acme")))

    result = extractor.extract(
        write_pdf(tmp_path / "invoice.pdf"), reference=reference
    )

    assert result.needs_review is False


def test_future_dated_invoice_is_flagged(tmp_path: Path) -> None:
    extractor, _ = extractor_for(llm_data(invoice_date=field("2999-01-01")))

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert any("in the future" in warning for warning in result.warnings)


# --- failures ----------------------------------------------------------------


class RateLimitError(Exception):
    status_code = 429


class AuthenticationError(Exception):
    status_code = 401


class BadRequestError(Exception):
    status_code = 400


class InternalServerError(Exception):
    status_code = 529


def test_provider_failures_are_classified(tmp_path: Path) -> None:
    pdf = write_pdf(tmp_path / "invoice.pdf")
    for error, expected in (
        (RateLimitError("slow down"), ExtractionServiceError),
        (InternalServerError("overloaded"), ExtractionServiceError),
        (AuthenticationError("bad key"), ExtractionConfigurationError),
        (BadRequestError("bad pdf"), ExtractionError),
        (TypeError("Could not resolve authentication method"), ExtractionConfigurationError),
    ):
        extractor, _ = extractor_for(error)
        with pytest.raises(expected):
            extractor.extract(pdf)


def test_refusal_truncation_and_bad_json_need_review(tmp_path: Path) -> None:
    pdf = write_pdf(tmp_path / "invoice.pdf")
    for response in (
        {"stop_reason": "refusal"},
        {"stop_reason": "max_tokens"},
        {"text": "not json"},
        {"text": "[1, 2]"},
    ):
        extractor, _ = extractor_for(llm_data(), **response)
        with pytest.raises(ExtractionError):
            extractor.extract(pdf)


def test_non_pdf_and_missing_files_are_rejected(tmp_path: Path) -> None:
    extractor, client = extractor_for(llm_data())

    with pytest.raises(ExtractionError, match="not found"):
        extractor.extract(tmp_path / "nope.pdf")
    with pytest.raises(ExtractionError, match="valid PDF"):
        extractor.extract(write_pdf(tmp_path / "x.pdf", b"hello"))
    assert client.messages.calls == []


# --- configuration -----------------------------------------------------------


def test_unconfigured_extraction_does_not_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EXTRACTION_PROVIDER", raising=False)

    result = run_ai_extraction(write_pdf(tmp_path / "invoice.pdf"))

    assert ai_extraction_configured() is False
    assert result.needs_review is True
    assert result.invoice_value is None
    assert "not configured" in result.warnings[0]


def test_configuration_follows_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXTRACTION_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-for-test")
    assert ai_extraction_configured() is True

    monkeypatch.setenv("EXTRACTION_PROVIDER", "none")
    assert ai_extraction_configured() is False

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("EXTRACTION_PROVIDER", "anthropic")
    assert ai_extraction_configured() is True


@pytest.mark.parametrize(
    "settings",
    [
        ExtractionSettings(provider="openai"),
        ExtractionSettings(model=" "),
        ExtractionSettings(effort="extreme"),
        ExtractionSettings(timeout_seconds=0),
    ],
)
def test_invalid_settings_are_rejected(settings: ExtractionSettings) -> None:
    with pytest.raises(ExtractionConfigurationError):
        ClaudeInvoiceExtractor(settings, client=FakeClient(llm_data()))


# --- lifecycle integration ----------------------------------------------------


def lifecycle_for(
    tmp_path: Path, extractor: ClaudeInvoiceExtractor, sharepoint: object | None = None
) -> tuple[InvoiceLifecycle, InvoiceStore, int]:
    pdf = write_pdf(tmp_path / "invoice.pdf")
    invoices = InvoiceStore(tmp_path / "invoices.db")
    record = invoices.add_from_outlook(
        message={"id": "message-1"},
        attachment={"id": "attachment-1", "name": "invoice.pdf", "size": 10},
        stored_path=pdf,
    )
    lifecycle = InvoiceLifecycle(
        invoices,
        IrjNumberGenerator(tmp_path / "invoices.db"),
        ActivityFeedStore(tmp_path / "activity.db"),
        sharepoint,  # type: ignore[arg-type]
        companies_store=CompanyStore(tmp_path / "config.db"),
        suppliers_store=SupplierStore(tmp_path / "config.db"),
        extraction_runner=extractor.extract,
    )
    return lifecycle, invoices, record.id


def test_lifecycle_persists_partial_extraction_and_warnings(tmp_path: Path) -> None:
    extractor, _ = extractor_for(
        llm_data(supplier_invoice_number=field(None, "low", None))
    )
    lifecycle, _, invoice_id = lifecycle_for(tmp_path, extractor)

    extracted = lifecycle.run_extraction(invoice_id)

    assert extracted.status == "Needs Review"
    assert extracted.company == "Acme Trading Ltd"
    assert extracted.supplier == "Supplier Ltd"
    assert extracted.supplier_invoice_number is None
    assert extracted.ai_confidence == 0.0
    assert extracted.extraction_model == "claude-opus-5-5"
    assert extracted.extraction_prompt_version == PROMPT_VERSION
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

    overrides: dict[str, Any] = {"invoice_value": field(1250.75, "medium")}
    if not has_po_number:
        overrides["purchase_order_number"] = field(None, "low", None)
    extractor, _ = extractor_for(llm_data(**overrides))
    sharepoint = FakeSharePointClient()
    lifecycle, invoices, invoice_id = lifecycle_for(tmp_path, extractor, sharepoint)
    invoices.update_fields(invoice_id, sharepoint_item_id="drive-item-1")

    extracted = lifecycle.run_extraction(invoice_id)

    assert extracted.status == "Needs Review"
    assert extracted.invoice_type == expected_type
    assert sharepoint.moves == [
        ("drive-item-1", "Invoices/Flagged Invoices", "invoice.pdf")
    ]


def test_extraction_immediately_flags_matching_supplier_invoice_number(
    tmp_path: Path,
) -> None:
    extractor, _ = extractor_for(llm_data())
    lifecycle, invoices, resent_id = lifecycle_for(tmp_path, extractor)
    original_pdf = write_pdf(tmp_path / "original.pdf", b"%PDF-1.7\noriginal\n%%EOF")
    original = invoices.add_from_outlook(
        message={"id": "message-0"},
        attachment={"id": "attachment-0", "name": "original.pdf"},
        stored_path=original_pdf,
    )
    invoices.update_fields(
        original.id,
        company="Acme Trading Ltd",
        supplier="Supplier Ltd",
        supplier_invoice_number="INV-1001",
        status="Awaiting Sage Registration",
    )

    duplicate = lifecycle.run_extraction(resent_id)

    assert duplicate.status == "Needs Review"
    assert duplicate.duplicate_of_invoice_id == original.id
    assert "matched on supplier invoice number" in str(duplicate.review_reason)


def test_extraction_cannot_overwrite_a_confirmed_invoice(tmp_path: Path) -> None:
    extractor, client = extractor_for(llm_data())
    lifecycle, invoices, invoice_id = lifecycle_for(tmp_path, extractor)
    invoices.update_fields(
        invoice_id, status="Approved", supplier_invoice_number="HUMAN-CONFIRMED"
    )

    with pytest.raises(ValueError, match="awaiting extraction"):
        lifecycle.run_extraction(invoice_id)

    assert invoices.get(invoice_id).supplier_invoice_number == "HUMAN-CONFIRMED"
    assert client.messages.calls == []


def test_temporary_provider_failure_is_retryable(tmp_path: Path) -> None:
    from app.invoice_lifecycle import InvoiceExtractionUnavailableError

    extractor, _ = extractor_for(RateLimitError("busy"))
    lifecycle, invoices, invoice_id = lifecycle_for(tmp_path, extractor)

    with pytest.raises(InvoiceExtractionUnavailableError):
        lifecycle.run_extraction(invoice_id)

    assert invoices.get(invoice_id).status == "Awaiting AI Extraction"


def test_default_runner_passes_the_configured_names_to_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(path: Path, *, reference: ReferenceData | None = None) -> object:
        captured["reference"] = reference
        raise ExtractionError("stop here")

    monkeypatch.setattr("app.invoice_lifecycle.run_ai_extraction", fake_run)
    pdf = write_pdf(tmp_path / "invoice.pdf")
    lifecycle = InvoiceLifecycle(
        InvoiceStore(tmp_path / "invoices.db"),
        IrjNumberGenerator(tmp_path / "invoices.db"),
        ActivityFeedStore(tmp_path / "activity.db"),
        None,
        companies_store=CompanyStore(tmp_path / "config.db"),
        suppliers_store=SupplierStore(tmp_path / "config.db"),
    )

    with pytest.raises(ExtractionError):
        lifecycle.extraction_runner(pdf)

    reference = captured["reference"]
    assert isinstance(reference, ReferenceData)
    assert "Acme Trading Ltd" in reference.known_company_names()
    assert "Acme" in reference.known_company_names()
