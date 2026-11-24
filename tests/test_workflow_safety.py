"""A failure part-way through a workflow step must not strand or rewind an invoice."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.activity_feed import ActivityFeedStore
from app.companies import CompanyStore
from app.invoice_lifecycle import InvoiceLifecycle, InvoiceLifecycleError
from app.invoices import InvoiceStore
from app.irj import IrjNumberGenerator
from app.suppliers import SupplierStore
from tests.test_api import client_for


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
    )
    return lifecycle, store, record.id


def _confirm(lifecycle: InvoiceLifecycle, invoice_id: int, **overrides: object) -> object:
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
    return lifecycle.confirm_and_route(invoice_id, **fields)


def test_failed_approval_move_returns_the_invoice_to_its_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)
    store.update_fields(
        invoice_id,
        status="Awaiting Approval 1",
        company="Acme Trading Ltd",
        supplier="Supplier Ltd",
        irj_number="000001",
        approver1_email="jordan.blake@example.test",
    )

    def broken_move(*args: object, **kwargs: object) -> None:
        raise OSError("local PDF cache is missing")

    monkeypatch.setattr(lifecycle, "_move_pdf_in_sharepoint", broken_move)

    with pytest.raises(OSError):
        lifecycle.decide_approval(
            invoice_id, level=1, decision="approved", comments=None, recorded_by="Jordan"
        )

    assert store.get(invoice_id).status == "Awaiting Approval 1"


def test_confirm_refuses_an_invoice_already_past_review(tmp_path: Path) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)
    store.update_fields(invoice_id, status="Approved", irj_number="000001")

    with pytest.raises(InvoiceLifecycleError, match="not awaiting confirmation"):
        _confirm(lifecycle, invoice_id)

    assert store.get(invoice_id).status == "Approved"


def test_failed_stage_email_does_not_fail_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)

    def failing_send(**kwargs: object) -> bool:
        raise RuntimeError("Graph is unavailable")

    monkeypatch.setattr("app.invoice_lifecycle.send_email_notification", failing_send)

    record = _confirm(lifecycle, invoice_id, purchase_order_number="PO-1")

    assert record.status == "Awaiting PO Matching"
    events = lifecycle.activity_feed.list_since(0)
    assert any(
        event.event_type == "email_notification_failed" and event.invoice_id == invoice_id
        for event in events
    )


def test_deleting_a_duplicate_that_another_invoice_points_at(tmp_path: Path) -> None:
    lifecycle, store, original_id = _lifecycle(tmp_path)
    duplicate = store.add_from_outlook(
        message={"id": "message-2"},
        attachment={"id": "attachment-2", "name": "copy.pdf"},
        stored_path=tmp_path / "copy.pdf",
    )
    later = store.add_from_outlook(
        message={"id": "message-3"},
        attachment={"id": "attachment-3", "name": "copy-again.pdf"},
        stored_path=tmp_path / "copy-again.pdf",
    )
    store.update_fields(
        duplicate.id, status="Needs Review", duplicate_of_invoice_id=original_id
    )
    store.update_fields(later.id, duplicate_of_invoice_id=duplicate.id)

    lifecycle.delete_invoice(duplicate.id, recorded_by="Purchase Ledger")

    assert store.get(duplicate.id) is None
    assert store.get(later.id).duplicate_of_invoice_id is None
    # The audit event must not reference the deleted row (PostgreSQL has a
    # foreign key from activity events to invoices).
    deleted = [
        event
        for event in lifecycle.activity_feed.list_since(0)
        if event.event_type == "invoice_deleted"
    ]
    assert len(deleted) == 1 and deleted[0].invoice_id is None
    assert f"#{duplicate.id}" in deleted[0].message


def test_supplier_bank_details_are_limited_to_purchase_ledger(tmp_path: Path) -> None:
    client: TestClient = client_for(tmp_path)
    params = {"company": "Acme Trading Ltd", "supplier": "Supplier Ltd"}

    client.post(
        "/api/auth/login",
        json={"username": "jordan.blake", "password": "ChangeMe-App1!"},
    )
    assert client.get("/api/supplier-terms", params=params).status_code == 403

    client.post(
        "/api/auth/login",
        json={"username": "purchase.ledger", "password": "ChangeMe-PL1!"},
    )
    assert client.get("/api/supplier-terms", params=params).status_code == 200
