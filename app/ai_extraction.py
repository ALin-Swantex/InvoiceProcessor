from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# PLACEHOLDER — AI FIELD EXTRACTION
# ---------------------------------------------------------------------------
# This module is the integration point for Azure AI Document Intelligence's
# prebuilt invoice model (see PROJECT_HANDOFF.md "AI recommendation").
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
#   # Map result.documents[0].fields into ExtractionResult below.
#
# Until that integration exists, this placeholder never guesses field
# values. It always reports zero confidence so invoices are routed to
# "Needs Review" for a human (Purchase Ledger) to confirm, matching the
# specification's requirement that the system must not guess.
# ---------------------------------------------------------------------------


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
    connected."""
    del pdf_path  # Unused until the real extraction call is wired in.
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
    )
