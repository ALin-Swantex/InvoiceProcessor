from __future__ import annotations

import json
import os
from io import BytesIO
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol


DEFAULT_CONFIDENCE_THRESHOLD = 0.80
DEFAULT_MODEL_ID = "prebuilt-invoice"

# These fields are required before automation may present an invoice as
# confidently extracted. A PO number is deliberately excluded: its absence is
# meaningful because it selects the nominal route, so Purchase Ledger confirms
# whether the invoice genuinely has no PO.
REQUIRED_FIELDS = (
    "company",
    "supplier",
    "supplier_invoice_number",
    "invoice_date",
    "invoice_value",
    "currency",
)


class DocumentIntelligenceConfigurationError(RuntimeError):
    pass


class DocumentIntelligenceError(RuntimeError):
    pass


class DocumentIntelligenceServiceError(RuntimeError):
    pass


class AnalysisClient(Protocol):
    def begin_analyze_document(
        self, model_id: str, *, body: Any
    ) -> Any: ...


@dataclass(frozen=True)
class DocumentIntelligenceSettings:
    endpoint: str
    model_id: str = DEFAULT_MODEL_ID

    def validate(self) -> None:
        if not self.endpoint.strip():
            raise DocumentIntelligenceConfigurationError(
                "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT is required."
            )
        if not self.endpoint.startswith("https://"):
            raise DocumentIntelligenceConfigurationError(
                "The Document Intelligence endpoint must use HTTPS."
            )
        if not self.model_id.strip():
            raise DocumentIntelligenceConfigurationError(
                "The Document Intelligence model ID is required."
            )


@dataclass(frozen=True)
class ExtractionResult:
    company: str | None
    supplier: str | None
    supplier_invoice_number: str | None
    purchase_order_number: str | None
    invoice_date: str | None
    invoice_value: float | None
    currency: str | None
    confidence: float
    needs_review: bool
    field_confidences: dict[str, float]
    warnings: tuple[str, ...]

    def field_confidences_json(self) -> str:
        return json.dumps(self.field_confidences, sort_keys=True)


def confidence_threshold() -> float:
    try:
        from app.config_db import get_setting

        raw = get_setting("ai_confidence_threshold", "") or ""
    except Exception:
        raw = ""
    if not raw:
        raw = os.environ.get("AI_CONFIDENCE_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_CONFIDENCE_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CONFIDENCE_THRESHOLD
    return min(max(value, 0.0), 1.0)


def meets_confidence_threshold(confidence: float) -> bool:
    return confidence >= confidence_threshold()


def settings_from_environment() -> DocumentIntelligenceSettings:
    return DocumentIntelligenceSettings(
        endpoint=os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "").strip(),
        model_id=os.environ.get(
            "AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID", DEFAULT_MODEL_ID
        ).strip(),
    )


def ai_extraction_configured() -> bool:
    return bool(settings_from_environment().endpoint)


class AzureInvoiceExtractor:
    def __init__(
        self,
        settings: DocumentIntelligenceSettings,
        *,
        client: AnalysisClient | None = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self.client = client or self._build_client(settings)

    def extract(self, pdf_path: Path) -> ExtractionResult:
        if not pdf_path.is_file():
            raise DocumentIntelligenceError(f"Invoice PDF was not found: {pdf_path}")
        pdf_bytes = pdf_path.read_bytes()
        if not pdf_bytes.startswith(b"%PDF-"):
            raise DocumentIntelligenceError("The invoice is not a valid PDF.")

        try:
            poller = self.client.begin_analyze_document(
                self.settings.model_id,
                body=BytesIO(pdf_bytes),
            )
            analysis = poller.result()
        except Exception as error:
            raise DocumentIntelligenceServiceError(
                f"Azure Document Intelligence analysis failed: {error}"
            ) from error

        documents = getattr(analysis, "documents", None) or []
        if not documents:
            raise DocumentIntelligenceError(
                "Azure Document Intelligence returned no invoice document."
            )
        fields = getattr(documents[0], "fields", None) or {}
        return self._map_fields(fields)

    @staticmethod
    def _build_client(settings: DocumentIntelligenceSettings) -> AnalysisClient:
        try:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.identity import ClientSecretCredential, DefaultAzureCredential
        except ImportError as error:
            raise DocumentIntelligenceConfigurationError(
                "Install the 'azure' optional dependencies before enabling "
                "Document Intelligence."
            ) from error
        tenant_id = os.environ.get("OUTLOOK_MCP_TENANT_ID", "").strip()
        client_id = os.environ.get("OUTLOOK_MCP_CLIENT_ID", "").strip()
        client_secret = os.environ.get("OUTLOOK_MCP_CLIENT_SECRET", "").strip()
        supplied_credentials = [tenant_id, client_id, client_secret]
        if any(supplied_credentials) and not all(supplied_credentials):
            raise DocumentIntelligenceConfigurationError(
                "The Invoice MCP tenant ID, client ID, and client secret must "
                "all be configured for Document Intelligence authentication."
            )
        credential = (
            ClientSecretCredential(tenant_id, client_id, client_secret)
            if tenant_id and client_id and client_secret
            else DefaultAzureCredential()
        )
        return DocumentIntelligenceClient(
            endpoint=settings.endpoint,
            credential=credential,
        )

    @staticmethod
    def _map_fields(fields: dict[str, Any]) -> ExtractionResult:
        mapped_fields = {
            "company": _string_value(fields.get("CustomerName")),
            "supplier": _string_value(fields.get("VendorName")),
            "supplier_invoice_number": _string_value(fields.get("InvoiceId")),
            "purchase_order_number": _string_value(fields.get("PurchaseOrder")),
            "invoice_date": _date_value(fields.get("InvoiceDate")),
            "invoice_value": _currency_amount(fields.get("InvoiceTotal")),
            "currency": _currency_code(fields.get("InvoiceTotal")),
        }
        confidences = {
            name: _confidence(fields.get(source_name))
            for name, source_name in {
                "company": "CustomerName",
                "supplier": "VendorName",
                "supplier_invoice_number": "InvoiceId",
                "purchase_order_number": "PurchaseOrder",
                "invoice_date": "InvoiceDate",
                "invoice_value": "InvoiceTotal",
                "currency": "InvoiceTotal",
            }.items()
            if fields.get(source_name) is not None
        }

        threshold = confidence_threshold()
        warnings: list[str] = []
        required_confidences: list[float] = []
        for name in REQUIRED_FIELDS:
            value = mapped_fields[name]
            if value is None or value == "":
                warnings.append(f"Required field '{_display_name(name)}' is missing.")
                required_confidences.append(0.0)
                continue
            field_confidence = confidences.get(name, 0.0)
            required_confidences.append(field_confidence)
            if field_confidence < threshold:
                warnings.append(
                    f"'{_display_name(name)}' confidence "
                    f"{field_confidence:.0%} is below the {threshold:.0%} threshold."
                )
        po_number = mapped_fields["purchase_order_number"]
        po_confidence = confidences.get("purchase_order_number", 0.0)
        if po_number and po_confidence < threshold:
            warnings.append(
                f"'purchase order number' confidence {po_confidence:.0%} is "
                f"below the {threshold:.0%} threshold."
            )

        overall_confidence = min(required_confidences, default=0.0)
        return ExtractionResult(
            company=mapped_fields["company"],
            supplier=mapped_fields["supplier"],
            supplier_invoice_number=mapped_fields["supplier_invoice_number"],
            purchase_order_number=mapped_fields["purchase_order_number"],
            invoice_date=mapped_fields["invoice_date"],
            invoice_value=mapped_fields["invoice_value"],
            currency=mapped_fields["currency"],
            confidence=overall_confidence,
            needs_review=bool(warnings),
            field_confidences=confidences,
            warnings=tuple(warnings),
        )


def run_ai_extraction(pdf_path: Path) -> ExtractionResult:
    """Analyze one invoice using Entra-authenticated Document Intelligence.

    With no endpoint configured, retain safe local behavior: do not guess any
    values and require Purchase Ledger review.
    """
    if not ai_extraction_configured():
        return ExtractionResult(
            company=None,
            supplier=None,
            supplier_invoice_number=None,
            purchase_order_number=None,
            invoice_date=None,
            invoice_value=None,
            currency=None,
            confidence=0.0,
            needs_review=True,
            field_confidences={},
            warnings=("Azure Document Intelligence is not configured.",),
        )
    return _extractor_from_settings(settings_from_environment()).extract(pdf_path)


@lru_cache(maxsize=4)
def _extractor_from_settings(
    settings: DocumentIntelligenceSettings,
) -> AzureInvoiceExtractor:
    return AzureInvoiceExtractor(settings)


def _field_value(field: Any) -> Any:
    if field is None:
        return None
    for attribute in (
        "value_string",
        "value_date",
        "value_currency",
        "value_number",
        "value_integer",
    ):
        value = getattr(field, attribute, None)
        if value is not None:
            return value
    return getattr(field, "content", None)


def _string_value(field: Any) -> str | None:
    value = _field_value(field)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _date_value(field: Any) -> str | None:
    value = _field_value(field)
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _currency_amount(field: Any) -> float | None:
    value = _field_value(field)
    amount = getattr(value, "amount", None)
    if isinstance(amount, (int, float)):
        return float(amount)
    return float(value) if isinstance(value, (int, float)) else None


def _currency_code(field: Any) -> str | None:
    value = _field_value(field)
    code = getattr(value, "currency_code", None)
    if not isinstance(code, str):
        return None
    normalized = code.strip().upper()
    return normalized if len(normalized) == 3 and normalized.isalpha() else None


def _confidence(field: Any) -> float:
    value = getattr(field, "confidence", 0.0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _display_name(name: str) -> str:
    return name.replace("_", " ")
