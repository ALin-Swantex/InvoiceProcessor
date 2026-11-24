"""Workflow steps must not overwrite newer state, leak IRJ numbers or strand PDFs."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.activity_feed import ActivityFeedStore
from app.ai_extraction import ExtractionResult
from app.companies import CompanyStore
from app.document_classification import DocumentClassification
from app.invoice_lifecycle import InvoiceLifecycle, InvoiceLifecycleError
from app.invoices import InvoiceStore
from app.irj import IrjNumberGenerator
from app.suppliers import SupplierStore


def _lifecycle(tmp_path: Path) -> tuple[InvoiceLifecycle, InvoiceStore, int]:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%%EOF")
    store = InvoiceStore(tmp_path / "invoices.db")
    record = store.add_from_outlook(
        message={"id": "message-1"},
        attachment={"id": "attachment-1", "name": "invoice.pdf"},
        stored_path=pdf,
    )
    lifecycle = InvoiceLifecycle(
        store,
        IrjNumberGenerator(tmp_path / "irj.db"),
        ActivityFeedStore(tmp_path / "activity.db"),
        None,
        companies_store=CompanyStore(tmp_path / "config.db"),
        suppliers_store=SupplierStore(tmp_path / "config.db"),
        classification_runner=lambda path: DocumentClassification(
            document_type="invoice", confidence=0.99, reason="Looks like an invoice."
        ),
    )
    return lifecycle, store, record.id


def _confirm_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "company": "Acme Trading Ltd",
        "supplier": "Supplier Ltd",
        "supplier_invoice_number": "INV-1",
        "purchase_order_number": None,
        "invoice_date": "2026-08-31",
        "invoice_value": 100.0,
        "currency": "GBP",
    }
    fields.update(overrides)
    return fields


def _result(**overrides: object) -> ExtractionResult:
    values: dict[str, object] = {
        "company": "Acme Trading Ltd",
        "supplier": "Supplier Ltd",
        "supplier_invoice_number": "INV-1",
        "purchase_order_number": None,
        "invoice_date": "2026-08-31",
        "invoice_value": 100.0,
        "currency": "GBP",
        "confidence": 0.99,
        "needs_review": False,
        "field_confidences": {},
        "warnings": (),
    }
    values.update(overrides)
    return ExtractionResult(**values)  # type: ignore[arg-type]


# 1. Races --------------------------------------------------------------------


def test_stale_extraction_does_not_overwrite_a_confirmed_invoice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)
    flagged_moves: list[int] = []
    original_move_to_flagged = lifecycle._move_to_flagged

    def record_flagged(invoice: object) -> None:
        flagged_moves.append(invoice.id)  # type: ignore[attr-defined]
        original_move_to_flagged(invoice)  # type: ignore[arg-type]

    monkeypatch.setattr(lifecycle, "_move_to_flagged", record_flagged)

    def extraction_while_purchase_ledger_confirms(path: Path) -> ExtractionResult:
        lifecycle.confirm_and_route(invoice_id, **_confirm_fields())
        return _result()

    lifecycle.extraction_runner = extraction_while_purchase_ledger_confirms

    with pytest.raises(InvoiceLifecycleError):
        lifecycle.run_extraction(invoice_id)

    record = store.get(invoice_id)
    assert record.status == "Awaiting Sage Registration"
    assert record.irj_number == "000001"
    assert flagged_moves == []


def test_two_concurrent_confirms_allocate_only_one_irj(tmp_path: Path) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)
    store.update_fields(invoice_id, status="Needs Review")
    generator = lifecycle.irj_generator
    real_generate = generator.generate
    calls: list[str] = []
    nested_errors: list[Exception] = []

    def generate_while_second_confirm_arrives(company: str | None = None) -> str:
        calls.append(str(company))
        if len(calls) == 1:
            try:
                lifecycle.confirm_and_route(invoice_id, **_confirm_fields())
            except InvoiceLifecycleError as error:
                nested_errors.append(error)
        return real_generate(company)

    generator.generate = generate_while_second_confirm_arrives  # type: ignore[method-assign]

    record = lifecycle.confirm_and_route(invoice_id, **_confirm_fields())

    assert len(calls) == 1
    assert len(nested_errors) == 1
    assert record.status == "Awaiting Sage Registration"
    assert record.irj_number == "000001"
    assert generator.current("Acme Trading Ltd") == "000001"


def test_failed_confirmation_returns_the_invoice_to_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)
    store.update_fields(invoice_id, status="Needs Review")

    def broken_move(*args: object, **kwargs: object) -> None:
        raise InvoiceLifecycleError("SharePoint filing failed")

    monkeypatch.setattr(lifecycle, "_move_pdf_in_sharepoint", broken_move)

    with pytest.raises(InvoiceLifecycleError, match="SharePoint filing failed"):
        lifecycle.confirm_and_route(invoice_id, **_confirm_fields())

    record = store.get(invoice_id)
    assert record.status == "Needs Review"
    assert record.irj_number == "000001"  # kept for the retry, not leaked


def test_failed_auto_confirm_does_not_overwrite_a_manual_confirm(
    tmp_path: Path,
) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)
    stale = store.update_fields(
        invoice_id,
        status="Needs Review",
        company="Acme Trading Ltd",
        supplier="Supplier Ltd",
        supplier_invoice_number="INV-1",
    )
    store.update_fields(invoice_id, status="Awaiting Sage Registration")

    returned = lifecycle._auto_confirm(stale, _result())

    assert returned.status == "Awaiting Sage Registration"
    assert store.get(invoice_id).status == "Awaiting Sage Registration"


# 2. Flagging for review ------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        "Workflow Issue / On Hold",
        "Processing Approval 1",
        "Rejected",
        "Cancelled - Duplicate",
        "Statement Filed",
        "Reconciled / Complete",
    ],
)
def test_flag_for_review_refuses_held_processing_and_finished_invoices(
    tmp_path: Path, status: str
) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)
    store.update_fields(
        invoice_id,
        status=status,
        company="Acme Trading Ltd",
        review_return_status="Awaiting Approval 1",
    )

    with pytest.raises(InvoiceLifecycleError, match="cannot be flagged"):
        lifecycle.flag_for_review(invoice_id, reason="Check this")

    record = store.get(invoice_id)
    assert record.status == status
    assert record.review_return_status == "Awaiting Approval 1"


def test_flag_for_review_still_holds_an_active_invoice(tmp_path: Path) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)
    store.update_fields(
        invoice_id, status="Awaiting Approval 1", company="Acme Trading Ltd"
    )

    record = lifecycle.flag_for_review(invoice_id, reason="Wrong cost centre")

    assert record.status == "Workflow Issue / On Hold"
    assert record.review_return_status == "Awaiting Approval 1"


def test_flag_for_review_from_extraction_goes_to_needs_review(tmp_path: Path) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)

    record = lifecycle.flag_for_review(invoice_id, reason="Unreadable scan")

    assert record.status == "Needs Review"
    assert record.review_reason == "Unreadable scan"


# 3. Canonical company and supplier names -----------------------------------


def test_confirm_stores_canonical_names_for_aliases(tmp_path: Path) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)

    record = lifecycle.confirm_and_route(
        invoice_id, **_confirm_fields(company="Acme", supplier="Supplier Limited")
    )

    assert record.company == "Acme Trading Ltd"
    assert record.supplier == "Supplier Ltd"


def test_duplicate_check_uses_canonical_names(tmp_path: Path) -> None:
    lifecycle, store, first_id = _lifecycle(tmp_path)
    store.update_fields(
        first_id,
        status="Awaiting Sage Registration",
        company="Acme Trading Ltd",
        supplier="Supplier Ltd",
        supplier_invoice_number="INV-1",
        irj_number="000001",
    )
    second = store.add_from_outlook(
        message={"id": "message-2"},
        attachment={"id": "attachment-2", "name": "again.pdf"},
        stored_path=tmp_path / "invoice.pdf",
    )

    record = lifecycle.confirm_and_route(
        second.id, **_confirm_fields(company="Acme", supplier="Supplier Limited")
    )

    assert record.status == "Needs Review"
    assert record.duplicate_of_invoice_id == first_id
    assert record.company == "Acme Trading Ltd"
    assert record.supplier == "Supplier Ltd"


# 4. Statement detection ------------------------------------------------------


def test_failed_statement_move_is_not_saved_as_an_invoice_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)
    lifecycle.classification_runner = lambda path: DocumentClassification(
        document_type="statement", confidence=0.97, reason="Statement of account."
    )
    attempts: list[int] = []

    def flaky_move(invoice: object) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise InvoiceLifecycleError("SharePoint filing failed: timeout")

    monkeypatch.setattr(lifecycle, "_move_to_flagged", flaky_move)

    with pytest.raises(InvoiceLifecycleError, match="timeout"):
        lifecycle.run_extraction(invoice_id)

    # Saved as a statement for review even though the PDF is still in
    # Incoming: left in "Awaiting AI Extraction" nothing would retry it.
    record = store.get(invoice_id)
    assert record.status == "Needs Review"
    assert record.document_type == "statement"


# 5. Rejection notice retries ------------------------------------------------


def test_rejection_notice_is_retried_only_for_recent_rejections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, store, recent_id = _lifecycle(tmp_path)
    now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
    old = store.add_from_outlook(
        message={"id": "message-2"},
        attachment={"id": "attachment-2", "name": "old.pdf"},
        stored_path=tmp_path / "invoice.pdf",
        created_at=(now - timedelta(days=200)).isoformat(),
    )
    old_po = store.add_from_outlook(
        message={"id": "message-3"},
        attachment={"id": "attachment-3", "name": "old-po.pdf"},
        stored_path=tmp_path / "invoice.pdf",
        created_at=(now - timedelta(days=200)).isoformat(),
    )
    store.update_fields(
        recent_id,
        status="Rejected",
        approver1_decision="rejected",
        approver1_date=(now - timedelta(days=1)).isoformat(),
    )
    store.update_fields(
        old.id,
        status="Rejected",
        approver1_decision="rejected",
        approver1_date=(now - timedelta(days=90)).isoformat(),
    )
    store.update_fields(old_po.id, status="Rejected", rejection_reason="Wrong PO")
    sent: list[str] = []
    monkeypatch.setattr(
        "app.invoice_lifecycle.send_email_notification",
        lambda *, recipient, subject, body: sent.append(subject) or True,
    )

    lifecycle.process_scheduled_notifications(now=now)

    assert sent == ["Invoice (invoice.pdf) rejected"]


# 6. Sage registration retry -------------------------------------------------


def test_sage_retry_refuses_a_different_irj(tmp_path: Path) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)
    lifecycle.configuration_getter = lambda key, default=None: (
        "manual" if key.startswith("irj_mode:") else default
    )
    store.update_fields(
        invoice_id,
        status="Needs Review",
        invoice_type="nominal",
        company="Acme Trading Ltd",
        supplier="Supplier Ltd",
        irj_number="000005",
        sage_registered_at="2026-11-20T10:00:00+00:00",
    )

    with pytest.raises(InvoiceLifecycleError, match="cannot be changed"):
        lifecycle.register_in_sage(
            invoice_id, irj_number="000006", recorded_by="Purchase Ledger"
        )

    assert store.get(invoice_id).irj_number == "000005"


# 7. Reverting a foreign payment route ---------------------------------------


def test_foreign_poa_route_can_be_reverted(tmp_path: Path) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)
    store.update_fields(
        invoice_id,
        status="Approved",
        company="Acme Trading Ltd",
        supplier="Supplier Ltd",
        irj_number="000001",
    )
    lifecycle.route_for_payment(
        invoice_id, route="foreign_poa", recorded_by="Purchase Ledger"
    )

    record = lifecycle.revert_foreign_payment_route(
        invoice_id, recorded_by="Purchase Ledger"
    )

    assert record.status == "Approved"
    assert record.payment_method is None
    assert record.is_foreign_payment is None
