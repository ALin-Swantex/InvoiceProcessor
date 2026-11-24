from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfReader, PdfWriter

from app.approval_matrix import ApprovalMatrixStore
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
        approval_matrix_store=ApprovalMatrixStore(tmp_path / "config.db"),
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

    def fake_run(
        path: Path,
        *,
        reference: ReferenceData | None = None,
        threshold: float | None = None,
    ) -> object:
        captured["reference"] = reference
        captured["threshold"] = threshold
        raise ExtractionError("stop here")

    monkeypatch.setattr("app.lifecycle.base.run_ai_extraction", fake_run)
    pdf = write_pdf(tmp_path / "invoice.pdf")
    lifecycle = InvoiceLifecycle(
        InvoiceStore(tmp_path / "invoices.db"),
        IrjNumberGenerator(tmp_path / "invoices.db"),
        ActivityFeedStore(tmp_path / "activity.db"),
        None,
        companies_store=CompanyStore(tmp_path / "config.db"),
        approval_matrix_store=ApprovalMatrixStore(tmp_path / "config.db"),
        suppliers_store=SupplierStore(tmp_path / "config.db"),
        # The Admin threshold comes from the lifecycle's own settings store.
        configuration_getter=lambda key, default: (
            "0.42" if key == "ai_confidence_threshold" else default
        ),
    )

    with pytest.raises(ExtractionError):
        lifecycle.extraction_runner(pdf)

    assert captured["threshold"] == 0.42
    reference = captured["reference"]
    assert isinstance(reference, ReferenceData)
    assert "Acme Trading Ltd" in reference.known_company_names()
    assert "Acme" in reference.known_company_names()


# --- escalation to a stronger model -----------------------------------------

FIRST = "claude-sonnet-5-5"
SECOND = "claude-opus-5-5"


class RoutedClient:
    """Answers per model, so a first and a second model can differ."""

    def __init__(self, answers: dict[str, dict[str, Any] | Exception]) -> None:
        self.answers = answers
        self.messages = self
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        answer = self.answers[kwargs["model"]]
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(answer))],
        )

    def models_called(self) -> list[str]:
        return [call["model"] for call in self.calls]


def cascade(
    first: dict[str, Any] | Exception, second: dict[str, Any] | Exception | None = None
) -> tuple[ClaudeInvoiceExtractor, RoutedClient]:
    answers: dict[str, dict[str, Any] | Exception] = {FIRST: first}
    if second is not None:
        answers[SECOND] = second
    client = RoutedClient(answers)
    settings = ExtractionSettings(model=FIRST, escalation_model=SECOND)
    return ClaudeInvoiceExtractor(settings, client=client), client


def test_confident_first_answer_never_calls_the_stronger_model(tmp_path: Path) -> None:
    extractor, client = cascade(llm_data(), llm_data())

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert client.models_called() == [FIRST]
    assert result.model == FIRST
    assert result.needs_review is False


def test_difficult_invoice_is_escalated_and_the_stronger_answer_is_used(
    tmp_path: Path,
) -> None:
    unsure = llm_data(supplier_invoice_number=field("INV-10O1", "low"))
    extractor, client = cascade(unsure, llm_data())

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert client.models_called() == [FIRST, SECOND]
    assert client.calls[0]["output_config"]["effort"] == "medium"
    assert client.calls[1]["output_config"]["effort"] == "high"
    assert result.supplier_invoice_number == "INV-1001"
    assert result.model == f"{FIRST} -> {SECOND}"
    assert result.needs_review is False  # a low-certainty first read is not a conflict


def test_missing_field_filled_in_by_the_stronger_model(tmp_path: Path) -> None:
    extractor, client = cascade(
        llm_data(invoice_date=field(None, "low", None)), llm_data()
    )

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert client.models_called() == [FIRST, SECOND]
    assert result.invoice_date == "2026-09-08"
    assert result.needs_review is False


def test_confident_disagreement_between_the_models_needs_review(
    tmp_path: Path,
) -> None:
    # Medium certainty triggers the escalation; the stronger model then reads the
    # total differently, which is a conflict worth a person's attention.
    first = llm_data(invoice_value=field(1250.75, "medium"))
    second = llm_data(invoice_value=field(1520.75, "high"))
    extractor, _ = cascade(first, second)

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert result.invoice_value == 1520.75  # the stronger model's reading
    assert result.needs_review is True
    assert any(
        "1250.75" in warning and "1520.75" in warning for warning in result.warnings
    )
    assert result.field_confidences["invoice_value"] <= 0.60


def test_a_field_below_the_threshold_is_warned_about_once_after_escalation(
    tmp_path: Path,
) -> None:
    # The stronger model also reads the total with only medium certainty, and
    # differently. The threshold rule is applied once, to the final answer.
    first = llm_data(invoice_value=field(1250.75, "medium"))
    second = llm_data(invoice_value=field(1520.75, "medium"))
    extractor, _ = cascade(first, second)

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    below = [warning for warning in result.warnings if "below the" in warning]
    assert len(below) == 1
    assert result.needs_review is True
    assert result.confidence == result.field_confidences["invoice_value"]


def test_unreadable_first_answer_is_escalated(tmp_path: Path) -> None:
    client = RoutedClient({FIRST: llm_data(), SECOND: llm_data()})
    original = client.create

    def first_answer_is_cut_off(**kwargs: Any) -> object:
        if kwargs["model"] == FIRST:
            client.calls.append(kwargs)
            return SimpleNamespace(stop_reason="max_tokens", content=[])
        return original(**kwargs)

    client.create = first_answer_is_cut_off  # type: ignore[method-assign]
    extractor = ClaudeInvoiceExtractor(
        ExtractionSettings(model=FIRST, escalation_model=SECOND), client=client
    )

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert client.models_called() == [FIRST, SECOND]
    assert result.needs_review is False
    assert result.model == f"{FIRST} -> {SECOND}"


def test_temporary_failure_of_the_first_model_is_retried_not_escalated(
    tmp_path: Path,
) -> None:
    extractor, client = cascade(RateLimitError("busy"), llm_data())

    with pytest.raises(ExtractionServiceError):
        extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert client.models_called() == [FIRST]


def test_unavailable_stronger_model_keeps_the_first_answer_for_review(
    tmp_path: Path,
) -> None:
    unsure = llm_data(supplier_invoice_number=field("INV-10O1", "low"))
    extractor, client = cascade(unsure, InternalServerError("overloaded"))

    result = extractor.extract(write_pdf(tmp_path / "invoice.pdf"))

    assert client.models_called() == [FIRST, SECOND]
    assert result.supplier_invoice_number == "INV-10O1"
    assert result.model == FIRST
    assert result.needs_review is True
    assert any(SECOND in warning and "not available" in warning for warning in result.warnings)


def test_both_models_failing_raises(tmp_path: Path) -> None:
    extractor, _ = cascade(BadRequestError("bad"), BadRequestError("still bad"))

    with pytest.raises(ExtractionError):
        extractor.extract(write_pdf(tmp_path / "invoice.pdf"))


def test_statements_and_credit_notes_are_not_escalated(tmp_path: Path) -> None:
    for kind in ("statement", "credit_note"):
        extractor, client = cascade(
            llm_data(document_type=kind, document_type_reason="because"), llm_data()
        )
        extractor.extract(write_pdf(tmp_path / f"{kind}.pdf"))
        assert client.models_called() == [FIRST], kind


def test_escalation_is_off_unless_a_different_model_is_named() -> None:
    assert not ExtractionSettings().escalation_enabled
    assert not ExtractionSettings(escalation_model=" ").escalation_enabled
    assert not ExtractionSettings(escalation_model="claude-opus-5-5").escalation_enabled
    assert ExtractionSettings(
        model=FIRST, escalation_model=SECOND
    ).escalation_enabled


def test_escalation_settings_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai_extraction import settings_from_environment

    monkeypatch.setenv("EXTRACTION_MODEL", FIRST)
    monkeypatch.setenv("EXTRACTION_ESCALATION_MODEL", SECOND)
    monkeypatch.setenv("EXTRACTION_ESCALATION_EFFORT", "max")

    settings = settings_from_environment()

    assert (settings.model, settings.escalation_model) == (FIRST, SECOND)
    assert settings.escalation_effort == "max"
    with pytest.raises(ExtractionConfigurationError):
        ClaudeInvoiceExtractor(
            ExtractionSettings(escalation_effort="extreme"),
            client=FakeClient(llm_data()),
        )


# --- automatic confirmation of confident invoices -------------------------------


def _with_auto_confirm(lifecycle: InvoiceLifecycle, enabled: bool) -> list[dict[str, Any]]:
    """Switch the setting and record confirm_and_route calls instead of routing."""
    lifecycle.configuration_getter = lambda key, default=None: (
        "true" if enabled and key == "ai_auto_confirm" else default
    )
    calls: list[dict[str, Any]] = []

    def fake_confirm(invoice_id: int, **kwargs: Any) -> Any:
        calls.append({"invoice_id": invoice_id, **kwargs})
        return lifecycle.invoice_store.update_fields(invoice_id, status="Awaiting PO Matching")

    lifecycle.confirm_and_route = fake_confirm  # type: ignore[method-assign]
    return calls


def test_confident_invoice_is_confirmed_automatically_when_switched_on(
    tmp_path: Path,
) -> None:
    extractor, _ = extractor_for(llm_data())
    lifecycle, _, invoice_id = lifecycle_for(tmp_path, extractor)
    calls = _with_auto_confirm(lifecycle, True)

    result = lifecycle.run_extraction(invoice_id)

    assert result.status == "Awaiting PO Matching"
    assert len(calls) == 1
    assert calls[0]["recorded_by"] == "AI auto-confirm"
    assert calls[0]["supplier_invoice_number"] == "INV-1001"
    assert calls[0]["invoice_value"] == 1250.75


def test_confident_invoice_still_waits_for_review_when_switched_off(
    tmp_path: Path,
) -> None:
    extractor, _ = extractor_for(llm_data())
    lifecycle, _, invoice_id = lifecycle_for(tmp_path, extractor)
    calls = _with_auto_confirm(lifecycle, False)

    result = lifecycle.run_extraction(invoice_id)

    assert result.status == "Needs Review"
    assert calls == []


def test_doubtful_invoice_is_never_auto_confirmed(tmp_path: Path) -> None:
    extractor, _ = extractor_for(llm_data(invoice_value=field(1250.75, "medium")))
    lifecycle, _, invoice_id = lifecycle_for(tmp_path, extractor)
    calls = _with_auto_confirm(lifecycle, True)

    result = lifecycle.run_extraction(invoice_id)

    assert result.status == "Needs Review"
    assert calls == []


def test_refused_auto_confirmation_leaves_invoice_in_needs_review_with_reason(
    tmp_path: Path,
) -> None:
    from app.invoice_lifecycle import InvoiceLifecycleError

    extractor, _ = extractor_for(llm_data())
    lifecycle, _, invoice_id = lifecycle_for(tmp_path, extractor)
    _with_auto_confirm(lifecycle, True)

    def refuse(invoice_id: int, **kwargs: Any) -> Any:
        raise InvoiceLifecycleError("No approver is configured.")

    lifecycle.confirm_and_route = refuse  # type: ignore[method-assign]

    result = lifecycle.run_extraction(invoice_id)

    assert result.status == "Needs Review"
    assert "Automatic confirmation was not possible" in str(result.review_reason)
    assert "No approver is configured." in str(result.review_reason)


# --- matching printed names to the known names ---------------------------------


class SequenceMessages:
    """Replies in order: the extraction first, then any name double-checks."""

    def __init__(self, replies: list[dict[str, Any] | Exception]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(reply))],
        )


def _extract_names(
    tmp_path: Path,
    replies: list[dict[str, Any] | Exception],
    *,
    company: str = "Acme Trading Ltd",
    supplier: str = "Supplier Ltd",
    text_layer_name: str | None = None,
):
    messages = SequenceMessages(replies)
    client = SimpleNamespace(messages=messages)
    extractor = ClaudeInvoiceExtractor(ExtractionSettings(), client=client)
    reference = ReferenceData(
        companies=(("Acme Trading Ltd", ("Acme",)),),
        suppliers=(
            ("Smith & Sons Limited", ("Smiths",)),
            ("Smith Engineering Ltd", ()),
            ("Bolt Supplies Ltd", ()),
        ),
    )
    pdf = write_pdf(tmp_path / "invoice.pdf")
    result = extractor.extract(pdf, reference=reference)
    return result, messages


def test_names_that_differ_only_in_case_or_alias_become_the_exact_known_name(
    tmp_path: Path,
) -> None:
    data = llm_data(
        company=field("ACME"), supplier=field("BOLT SUPPLIES LTD")
    )
    result, messages = _extract_names(tmp_path, [data])

    assert result.company == "Acme Trading Ltd"
    assert result.supplier == "Bolt Supplies Ltd"
    assert len(messages.calls) == 1  # no second look needed


@pytest.mark.parametrize(
    "printed",
    ["Smith and Sons Ltd", "smith & sons limited.", "The Smith & Sons Ltd"],
)
def test_ampersand_and_legal_form_differences_resolve_without_the_model(
    tmp_path: Path, printed: str
) -> None:
    result, messages = _extract_names(
        tmp_path, [llm_data(supplier=field(printed))]
    )

    assert result.supplier == "Smith & Sons Limited"
    assert len(messages.calls) == 1


def test_a_close_but_unmatched_name_is_checked_and_must_be_an_exact_known_name(
    tmp_path: Path,
) -> None:
    extraction = llm_data(supplier=field("Boltt Suplies"))
    answer = {"match": "Bolt Supplies Ltd", "certainty": "high"}
    result, messages = _extract_names(tmp_path, [extraction, answer])

    assert result.supplier == "Bolt Supplies Ltd"
    check = messages.calls[1]
    schema = check["output_config"]["format"]["schema"]
    assert "Bolt Supplies Ltd" in schema["properties"]["match"]["enum"]
    assert "NONE" in schema["properties"]["match"]["enum"]
    assert check["model"] == "claude-opus-5-5"


def test_a_model_answer_outside_the_known_names_is_rejected(tmp_path: Path) -> None:
    extraction = llm_data(supplier=field("Boltt Suplies"))
    answer = {"match": "Bolt Supplies Limited", "certainty": "high"}  # not exact
    result, _ = _extract_names(tmp_path, [extraction, answer])

    assert result.supplier == "Boltt Suplies"  # kept as printed, then warned about


def test_the_model_saying_none_keeps_the_printed_name(tmp_path: Path) -> None:
    extraction = llm_data(supplier=field("Boltt Suplies"))
    result, _ = _extract_names(
        tmp_path, [extraction, {"match": "NONE", "certainty": "high"}]
    )

    assert result.supplier == "Boltt Suplies"


def test_an_unsure_name_match_lowers_the_confidence_below_the_threshold(
    tmp_path: Path,
) -> None:
    extraction = llm_data(supplier=field("Boltt Suplies"))
    result, _ = _extract_names(
        tmp_path, [extraction, {"match": "Bolt Supplies Ltd", "certainty": "medium"}]
    )

    assert result.supplier == "Bolt Supplies Ltd"
    assert result.field_confidences["supplier"] == CERTAINTY_SCORES["medium"]
    assert result.needs_review is True


def test_a_failed_name_check_does_not_fail_the_extraction(tmp_path: Path) -> None:
    extraction = llm_data(supplier=field("Boltt Suplies"))
    result, _ = _extract_names(
        tmp_path, [extraction, ExtractionServiceError("temporarily unavailable")]
    )

    assert result.supplier == "Boltt Suplies"
    assert result.document_type == "invoice"


def test_a_name_with_nothing_similar_is_not_sent_to_the_model(tmp_path: Path) -> None:
    extraction = llm_data(supplier=field("Zenith Quarries Group"))
    result, messages = _extract_names(tmp_path, [extraction])

    assert result.supplier == "Zenith Quarries Group"
    assert len(messages.calls) == 1


def test_ambiguous_normalised_names_go_to_the_model_not_a_guess(
    tmp_path: Path,
) -> None:
    from app.ai_extraction import resolve_known_name

    entries = (("Acme Ltd", ()), ("Acme Plc", ()))
    canonical, candidates = resolve_known_name("ACME", entries)

    assert canonical is None
    assert set(candidates) == {"Acme Ltd", "Acme Plc"}


def test_supplier_is_checked_against_the_document_as_printed_not_as_registered(
    tmp_path: Path,
) -> None:
    extractor, _ = extractor_for(llm_data())
    reference = ReferenceData(suppliers=(("Smith & Sons Limited", ()),))
    text = "Invoice from SMITH AND SONS LTD invoice number INV-1001 total 1250.75 " * 3

    result = extractor._read(
        llm_data(
            supplier=field("Smith and Sons Ltd"),
            supplier_invoice_number=field("INV-1001"),
        ),
        text_layer=text,
        reference=reference,
        model="claude-opus-5-5",
    )

    assert result.supplier == "Smith & Sons Limited"
    assert not any("supplier name" in warning for warning in result.warnings)


# --- grounding: values must appear in the document as values, not as digits
# --- buried inside other numbers ----------------------------------------------


def _grounding_warnings(values: dict[str, Any], text: str, **kwargs: Any) -> list[str]:
    from app.extraction_checks import run_checks

    return run_checks(values=values, text_layer=text, **kwargs).warnings


INVOICE_TEXT = (
    "Tax invoice from Supplier Ltd. Bill to Acme Trading Ltd. Invoice no INV-1001. "
    "Tel 0115 500 0123. Net 1,042.29 VAT 208.46 Total payable £1,250.75 GBP."
)


def test_a_total_hidden_in_other_digits_is_not_found() -> None:
    warnings = _grounding_warnings({"invoice_value": 50.00}, INVOICE_TEXT)

    assert any("50.00 was not found" in warning for warning in warnings)


@pytest.mark.parametrize(
    "text",
    [INVOICE_TEXT, INVOICE_TEXT.replace("£1,250.75", "EUR 1.250,75"), "Total 1250.75 " * 5],
)
def test_a_printed_total_is_found_in_any_common_format(text: str) -> None:
    assert _grounding_warnings({"invoice_value": 1250.75}, text) == []


def test_an_invoice_number_must_appear_as_a_whole_reference() -> None:
    warnings = _grounding_warnings({"supplier_invoice_number": "22"}, INVOICE_TEXT)

    assert any("'22' was not found" in warning for warning in warnings)


def test_an_invoice_number_with_different_separators_is_found() -> None:
    text = INVOICE_TEXT.replace("INV-1001", "INV 1001")

    assert _grounding_warnings({"supplier_invoice_number": "INV-1001"}, text) == []


def test_a_company_not_named_in_the_document_is_flagged() -> None:
    text = INVOICE_TEXT.replace("Acme Trading Ltd", "Riverside Logistics")

    warnings = _grounding_warnings(
        {"company": "Acme Trading Ltd"},
        text,
        company_names=("Acme Trading Ltd", "Acme"),
    )

    assert any("'Acme Trading Ltd' was not found" in warning for warning in warnings)


def test_a_company_named_by_its_alias_is_found() -> None:
    text = INVOICE_TEXT.replace("Acme Trading Ltd", "ACME")

    assert (
        _grounding_warnings(
            {"company": "Acme Trading Ltd"},
            text,
            company_names=("Acme Trading Ltd", "Acme"),
        )
        == []
    )


def test_a_company_chosen_by_the_name_check_goes_to_review(tmp_path: Path) -> None:
    # The printed company is not a known one; the second model call picks the
    # closest known name. That choice must be confirmed by a person.
    extraction = llm_data(company=field("Acme Trading (Scotland) Ltd"))
    answer = {"match": "Acme Trading Ltd", "certainty": "high"}
    result, _ = _extract_names(tmp_path, [extraction, answer])

    assert result.company == "Acme Trading Ltd"
    assert result.needs_review is True
    assert any("chosen from the known companies" in w for w in result.warnings)


def test_auto_confirm_uses_the_real_routing_and_holds_duplicates(
    tmp_path: Path,
) -> None:
    from app.approval_matrix import ApprovalMatrixStore

    extractor, _ = extractor_for(llm_data())
    lifecycle, invoices, invoice_id = lifecycle_for(tmp_path, extractor)
    approvals = ApprovalMatrixStore(tmp_path / "config.db")
    # The store seeds a route for this company and supplier.
    assert approvals.find("Acme Trading Ltd", "Supplier Ltd") is not None
    lifecycle.approval_matrix_store = approvals
    lifecycle.configuration_getter = lambda key, default=None: (
        "true" if key == "ai_auto_confirm" else default
    )

    routed = lifecycle.run_extraction(invoice_id)

    assert routed.status == "Awaiting PO Matching"
    assert routed.irj_number
    assert routed.reviewed_by == "AI auto-confirm"

    # The same invoice arriving again must wait for Purchase Ledger.
    second_pdf = write_pdf(tmp_path / "again.pdf")
    again = invoices.add_from_outlook(
        message={"id": "message-2"},
        attachment={"id": "attachment-2", "name": "again.pdf", "size": 10},
        stored_path=second_pdf,
    )

    held = lifecycle.run_extraction(again.id)

    assert held.status == "Needs Review"
    assert held.duplicate_of_invoice_id == invoice_id
    assert held.irj_number is None  # no IRJ number allocated to a duplicate
    assert "duplicate" in str(held.review_reason)
