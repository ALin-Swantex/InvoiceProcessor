from __future__ import annotations

import json
import os
import re
from io import BytesIO
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Protocol

from app.document_classification import classify_document_text


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
    document_type: str = "invoice"
    document_classification_confidence: float | None = None
    document_classification_reason: str | None = None

    def field_confidences_json(self) -> str:
        return json.dumps(self.field_confidences, sort_keys=True)


def confidence_threshold(
    setting_getter: Callable[[str, str | None], str | None] | None = None,
) -> float:
    try:
        if setting_getter is None:
            from app.config_db import get_setting

            setting_getter = get_setting
        raw = setting_getter("ai_confidence_threshold", "") or ""
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

        classification = classify_document_text(
            str(getattr(analysis, "content", "") or "")
        )
        if classification.document_type == "statement":
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
                warnings=(classification.reason,),
                document_type="statement",
                document_classification_confidence=classification.confidence,
                document_classification_reason=classification.reason,
            )

        documents = getattr(analysis, "documents", None) or []
        if not documents:
            raise DocumentIntelligenceError(
                "Azure Document Intelligence returned no invoice document."
            )
        fields = getattr(documents[0], "fields", None) or {}
        result = self._map_fields(
            fields,
            content=str(getattr(analysis, "content", "") or ""),
        )
        return ExtractionResult(
            **{
                **result.__dict__,
                "document_classification_confidence": classification.confidence,
                "document_classification_reason": classification.reason,
            }
        )

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
    def _map_fields(
        fields: dict[str, Any],
        *,
        content: str = "",
    ) -> ExtractionResult:
        invoice_id_field = fields.get("InvoiceId") or fields.get("InvoiceNumber")
        invoice_total_field = fields.get("InvoiceTotal")
        fallback_total_field = None
        if _currency_amount(invoice_total_field) is None:
            fallback_total_field = fields.get("AmountDue")
            if _currency_amount(fallback_total_field) is not None:
                invoice_total_field = fallback_total_field

        supplier_invoice_number = _string_value(invoice_id_field)
        invoice_value = _currency_amount(invoice_total_field)
        currency = (
            _currency_code(invoice_total_field)
            or _currency_code(fields.get("InvoiceCurrency"))
        )
        fallback_warnings: list[str] = []

        if supplier_invoice_number is None:
            supplier_invoice_number = _ocr_invoice_number(content)
            if supplier_invoice_number is not None:
                fallback_warnings.append(
                    "Supplier invoice number was recovered from OCR text and "
                    "requires manual confirmation."
                )

        ocr_amount, ocr_currency = _ocr_labelled_total(content)
        if invoice_value is None and ocr_amount is not None:
            invoice_value = ocr_amount
            fallback_warnings.append(
                "Invoice value was recovered from a labelled OCR total and "
                "requires manual confirmation."
            )
        if currency is None and ocr_currency is not None:
            currency = ocr_currency
            fallback_warnings.append(
                "Currency was recovered from a labelled OCR total and "
                "requires manual confirmation."
            )
        if fallback_total_field is not None:
            fallback_warnings.append(
                "Invoice value was taken from Azure's amount-due field because "
                "an invoice-total value was unavailable; confirm it manually."
            )

        mapped_fields = {
            "company": _string_value(fields.get("CustomerName")),
            "supplier": _string_value(fields.get("VendorName")),
            "supplier_invoice_number": supplier_invoice_number,
            "purchase_order_number": _string_value(fields.get("PurchaseOrder")),
            "invoice_date": _date_value(fields.get("InvoiceDate")),
            "invoice_value": invoice_value,
            "currency": currency,
        }
        confidences = {
            "company": _confidence(fields.get("CustomerName")),
            "supplier": _confidence(fields.get("VendorName")),
            "supplier_invoice_number": _confidence(invoice_id_field),
            "purchase_order_number": _confidence(fields.get("PurchaseOrder")),
            "invoice_date": _confidence(fields.get("InvoiceDate")),
            "invoice_value": (
                0.0 if fallback_total_field is not None else _confidence(invoice_total_field)
            ),
            "currency": max(
                _confidence(invoice_total_field),
                _confidence(fields.get("InvoiceCurrency")),
            ),
        }
        confidences = {
            name: value
            for name, value in confidences.items()
            if value > 0.0
        }

        threshold = confidence_threshold()
        warnings: list[str] = list(fallback_warnings)
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
    if isinstance(value, (int, float)):
        return float(value)
    return _parse_amount(str(value)) if value is not None else None


def _currency_code(field: Any) -> str | None:
    value = _field_value(field)
    code = getattr(value, "currency_code", None)
    if isinstance(code, str):
        normalized = code.strip().upper()
        if len(normalized) == 3 and normalized.isalpha():
            return normalized
    text = str(value or "")
    code_match = re.search(r"\b(GBP|EUR|USD)\b", text, re.IGNORECASE)
    if code_match:
        return code_match.group(1).upper()
    for symbol, currency in (("£", "GBP"), ("€", "EUR"), ("$", "USD")):
        if symbol in text:
            return currency
    return None


def _ocr_invoice_number(content: str) -> str | None:
    match = re.search(
        r"(?im)^[ \t]*invoice[ \t]*(?:no(?:\.|number)?|#)"
        r"[ \t:.-]*(?:\r?\n[ \t]*)?([A-Z0-9][A-Z0-9./-]{1,127})[ \t]*$",
        content,
    )
    return match.group(1).strip() if match else None


def _ocr_labelled_total(content: str) -> tuple[float | None, str | None]:
    match = re.search(
        r"(?im)^[ \t]*(?:invoice[ \t]+total|grand[ \t]+total|total[ \t]+due|"
        r"amount[ \t]+due)[ \t:.-]*(?:\r?\n[ \t]*)?"
        r"(?:(GBP|EUR|USD|£|€|\$)[ \t]*)?"
        r"([0-9][0-9., ]*)"
        r"(?:[ \t]*(GBP|EUR|USD|£|€|\$))?[ \t]*$",
        content,
    )
    if not match:
        return None, None
    amount = _parse_amount(match.group(2))
    currency_token = match.group(1) or match.group(3)
    currency = _currency_code_from_token(currency_token)
    return amount, currency


def _parse_amount(value: str) -> float | None:
    match = re.search(r"-?[0-9][0-9., ]*", value)
    if not match:
        return None
    normalized = match.group(0).replace(" ", "")
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif "," in normalized:
        decimal_digits = len(normalized) - normalized.rfind(",") - 1
        normalized = (
            normalized.replace(",", ".")
            if decimal_digits == 2
            else normalized.replace(",", "")
        )
    try:
        return float(normalized)
    except ValueError:
        return None


def _currency_code_from_token(token: str | None) -> str | None:
    if not token:
        return None
    return {
        "£": "GBP",
        "€": "EUR",
        "$": "USD",
    }.get(token, token.upper())


def _confidence(field: Any) -> float:
    value = getattr(field, "confidence", 0.0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _display_name(name: str) -> str:
    return name.replace("_", " ")
