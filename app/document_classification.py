from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True)
class DocumentClassification:
    document_type: str
    confidence: float
    reason: str


_STRONG_STATEMENT_PHRASES = (
    "statement of account",
    "account statement",
    "supplier statement",
    "monthly statement",
)
_SUPPORTING_STATEMENT_PHRASES = (
    "balance brought forward",
    "brought forward balance",
    "opening balance",
    "closing balance",
    "account summary",
    "aged balance",
    "aged debt",
    "current month",
)
_TRANSACTION_COLUMN_PHRASES = (
    "invoice date",
    "invoice number",
    "document number",
    "transaction date",
    "debit",
    "credit",
)


def classify_document_text(text: str) -> DocumentClassification:
    normalized = re.sub(r"\s+", " ", text).strip().casefold()
    strong = [phrase for phrase in _STRONG_STATEMENT_PHRASES if phrase in normalized]
    supporting = [
        phrase for phrase in _SUPPORTING_STATEMENT_PHRASES if phrase in normalized
    ]
    transaction_columns = [
        phrase for phrase in _TRANSACTION_COLUMN_PHRASES if phrase in normalized
    ]

    statement_score = min(
        1.0,
        (0.75 if strong else 0.0)
        + min(len(supporting), 2) * 0.10
        + min(len(transaction_columns), 2) * 0.05,
    )
    if statement_score >= 0.80:
        evidence = strong + supporting + transaction_columns
        return DocumentClassification(
            document_type="statement",
            confidence=statement_score,
            reason=f"Statement indicators detected: {', '.join(evidence[:5])}.",
        )
    return DocumentClassification(
        document_type="invoice",
        confidence=max(0.0, 1.0 - statement_score),
        reason="No strong supplier-statement pattern was detected.",
    )


def classify_pdf_document(pdf_path: Path) -> DocumentClassification:
    try:
        reader = PdfReader(pdf_path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        text = ""
    return classify_document_text(text)
