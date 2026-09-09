from pathlib import Path
from typing import cast

from app.activity_feed import ActivityFeedStore
from app.companies import CompanyStore
from app.document_classification import (
    DocumentClassification,
    classify_document_text,
)
from app.invoice_lifecycle import InvoiceLifecycle
from app.invoices import InvoiceStore
from app.irj import IrjNumberGenerator
from app.sharepoint import SharePointClient
from tests.pdf_helpers import VALID_PDF_BYTES


def test_statement_of_account_is_classified_as_statement() -> None:
    result = classify_document_text(
        """
        STATEMENT OF ACCOUNT
        Opening balance 1,250.00
        Invoice date | Invoice number | Debit | Credit
        Closing balance 875.00
        """
    )

    assert result.document_type == "statement"
    assert result.confidence >= 0.80
    assert "statement of account" in result.reason


def test_single_invoice_is_not_misclassified_from_balance_due() -> None:
    result = classify_document_text(
        "Tax Invoice INV-1001 Invoice date 9 September 2026 Balance due £120.00"
    )

    assert result.document_type == "invoice"


class StatementSharePointClient:
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

    def get_item_web_url(self, item: dict[str, object]) -> str | None:
        return str(item["webUrl"])


def _statement_lifecycle(tmp_path: Path):
    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(VALID_PDF_BYTES)
    store = InvoiceStore(tmp_path / "invoices.db")
    document = store.add_from_outlook(
        message={"id": "message-1"},
        attachment={
            "id": "attachment-1",
            "name": "statement.pdf",
            "sharepoint_item_id": "item-1",
        },
        stored_path=pdf,
    )
    sharepoint = StatementSharePointClient()
    lifecycle = InvoiceLifecycle(
        store,
        IrjNumberGenerator(tmp_path / "invoices.db"),
        ActivityFeedStore(tmp_path / "activity.db"),
        cast(SharePointClient, sharepoint),
        companies_store=CompanyStore(tmp_path / "config.db"),
        extraction_runner=lambda path: (_ for _ in ()).throw(
            AssertionError("Invoice extraction must not run for a detected statement")
        ),
        classification_runner=lambda path: DocumentClassification(
            "statement", 0.95, "Statement indicators detected."
        ),
    )
    return lifecycle, store, document, sharepoint


def test_detected_statement_is_flagged_then_manually_filed(tmp_path: Path) -> None:
    lifecycle, store, document, sharepoint = _statement_lifecycle(tmp_path)

    flagged = lifecycle.run_extraction(document.id)
    filed = lifecycle.file_statement(
        document.id,
        company="Acme Trading Ltd",
        recorded_by="Purchase Ledger",
    )

    assert flagged.document_type == "statement"
    assert flagged.status == "Needs Review"
    assert filed.status == "Statement Filed"
    assert filed.company == "Acme Trading Ltd"
    assert filed.irj_number is None
    assert sharepoint.moves == [
        ("item-1", "Statements/Acme Trading Ltd", "statement.pdf")
    ]
    assert store.get(document.id) == filed


def test_detected_statement_can_be_returned_to_invoice_review(
    tmp_path: Path,
) -> None:
    lifecycle, _, document, _ = _statement_lifecycle(tmp_path)
    lifecycle.run_extraction(document.id)

    record = lifecycle.mark_as_invoice(
        document.id, recorded_by="Purchase Ledger"
    )

    assert record.document_type == "invoice"
    assert record.status == "Needs Review"
    assert "invoice confirmation" in str(record.review_reason)
