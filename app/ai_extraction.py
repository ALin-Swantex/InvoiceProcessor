from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# PLACEHOLDER — AI FIELD EXTRACTION
# ---------------------------------------------------------------------------
# This module is the integration point for Azure AI Document Intelligence's
# prebuilt invoice model (see PROJECT_HANDOFF.md "AI recommendation").
#
# Required environment variables once connected (see .env.example):
#   AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT — the resource endpoint URL
#   AZURE_DOCUMENT_INTELLIGENCE_KEY      — the resource API key
#   AI_CONFIDENCE_THRESHOLD              — minimum per-field confidence
#                                          (0.0-1.0) required before a field
#                                          is trusted rather than sent to
#                                          "Needs Review" (default 0.80)
#
# Replace `run_ai_extraction` with a real call, for example using the
# `azure-ai-documentintelligence` SDK:
#
#   from azure.ai.documentintelligence import DocumentIntelligenceClient
#   from azure.core.credentials import AzureKeyCredential
#
#   client = DocumentIntelligenceClient(endpoint, AzureKeyCredential(key))
#   poller = client.begin_analyze_document(
#       "prebuilt-invoice", document=pdf_bytes
#   )
#   result = poller.result()
#   # Map result.documents[0].fields into ExtractionResult below, using
#   # min(field.confidence for field in required_fields) as `confidence`,
#   # then call meets_confidence_threshold(confidence) to decide
#   # needs_review, exactly as this placeholder does.
#
# Until that integration exists, this placeholder never guesses field
# values. It always reports zero confidence so invoices are routed to
# "Needs Review" for a human (Purchase Ledger) to confirm, matching the
# specification's requirement that the system must not guess.
# ---------------------------------------------------------------------------

DEFAULT_CONFIDENCE_THRESHOLD = 0.80


def confidence_threshold() -> float:
    raw = os.environ.get("AI_CONFIDENCE_THRESHOLD", "").strip()
    if not raw:
        try:
            from app.config_db import get_setting

            raw = get_setting("ai_confidence_threshold", "") or ""
        except Exception:
            raw = ""
    if not raw:
        return DEFAULT_CONFIDENCE_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CONFIDENCE_THRESHOLD
    return min(max(value, 0.0), 1.0)


def meets_confidence_threshold(confidence: float) -> bool:
    return confidence >= confidence_threshold()


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


def run_ai_extraction(pdf_path: Path) -> ExtractionResult:
    """Placeholder AI extraction. Always returns zero-confidence, empty
    fields, and needs_review=True until Azure AI Document Intelligence is
    connected (AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT / _KEY in .env.example).
    Once connected, needs_review should be
    `not meets_confidence_threshold(confidence)`."""
    del pdf_path  # Unused until the real extraction call is wired in.
    confidence = 0.0
    return ExtractionResult(
        company=None,
        supplier=None,
        supplier_invoice_number=None,
        purchase_order_number=None,
        invoice_date=None,
        invoice_value=None,
        currency=None,
        confidence=confidence,
        needs_review=not meets_confidence_threshold(confidence),
    )
