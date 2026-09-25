from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.activity_feed import ActivityFeedStore
from app.companies import CompanyStore
from app.invoice_lifecycle import InvoiceLifecycle
from app.invoices import InvoiceStore
from app.irj import IrjNumberGenerator
from app.supplier_terms import SupplierTermsStore


def lifecycle_with_invoice(tmp_path: Path) -> tuple[InvoiceLifecycle, InvoiceStore, int]:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%%EOF")
    store = InvoiceStore(tmp_path / "invoices.db")
    invoice = store.add_from_outlook(
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
        supplier_terms_store=SupplierTermsStore(tmp_path / "config.db"),
    )
    return lifecycle, store, invoice.id


def test_approval_reminders_repeat_after_seven_days_and_holds_after_thirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, store, invoice_id = lifecycle_with_invoice(tmp_path)
    sent: list[str] = []
    monkeypatch.setattr(
        "app.invoice_lifecycle.send_email_notification",
        lambda *, recipient, subject, body: sent.append(body) or True,
    )
    store.update_fields(
        invoice_id,
        status="Awaiting Approval 1",
        irj_number="000001",
        supplier="Supplier Ltd",
        approver1_email="approver@example.test",
        approval_requested_at="2026-01-01T00:00:00+00:00",
    )

    assert lifecycle.process_scheduled_notifications(
        now=datetime(2026, 1, 8, tzinfo=timezone.utc)
    ) == 1
    assert "?tab=approver1" in sent[-1]
    assert lifecycle.process_scheduled_notifications(
        now=datetime(2026, 1, 14, tzinfo=timezone.utc)
    ) == 0
    assert lifecycle.process_scheduled_notifications(
        now=datetime(2026, 1, 15, tzinfo=timezone.utc)
    ) == 1

    store.update_fields(
        invoice_id,
        hold_level=1,
        approval_hold_started_at="2026-02-01T00:00:00+00:00",
        approval_hold_reminder_sent_at=None,
    )
    assert lifecycle.process_scheduled_notifications(
        now=datetime(2026, 3, 2, tzinfo=timezone.utc)
    ) == 0
    assert lifecycle.process_scheduled_notifications(
        now=datetime(2026, 3, 3, tzinfo=timezone.utc)
    ) == 1


def test_approved_invoice_uses_supplier_default_payment_route(tmp_path: Path) -> None:
    lifecycle, store, invoice_id = lifecycle_with_invoice(tmp_path)
    lifecycle.supplier_terms_store.upsert(
        company="Acme Trading Ltd",
        supplier="Supplier Ltd",
        default_payment_method="Bankline",
    )
    store.update_fields(
        invoice_id,
        status="Approved",
        company="Acme Trading Ltd",
        supplier="Supplier Ltd",
        irj_number="000002",
    )

    routed = lifecycle.auto_route_for_payment(invoice_id)

    assert routed.status == "Approved for Payment - Bankline"
    assert routed.payment_method == "Bankline"
    assert routed.payment_route_decided_by == "Automatic supplier payment routing"


def test_missing_payment_setting_places_approved_invoice_on_hold(tmp_path: Path) -> None:
    lifecycle, store, invoice_id = lifecycle_with_invoice(tmp_path)
    store.update_fields(
        invoice_id,
        status="Approved",
        company="Acme Trading Ltd",
        supplier="Supplier Ltd",
        irj_number="000003",
    )

    held = lifecycle.auto_route_for_payment(invoice_id)

    assert held.status == "Payment Routing Issue / On Hold"
    assert "payment method" in str(held.hold_reason).lower()


def test_rejection_emails_purchase_ledger_with_rejected_queue_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, store, invoice_id = lifecycle_with_invoice(tmp_path)
    sent: list[tuple[str, str]] = []
    monkeypatch.setenv("PURCHASE_LEDGER_NOTIFICATION_EMAIL", "ledger@example.test")
    monkeypatch.setattr(
        "app.invoice_lifecycle.send_email_notification",
        lambda *, recipient, subject, body: sent.append((recipient, body)) or True,
    )
    store.update_fields(
        invoice_id,
        status="Awaiting PO Matching",
        company="Acme Trading Ltd",
        supplier="Supplier Ltd",
        irj_number="000004",
    )

    lifecycle.reject_invoice(
        invoice_id, reason="PO does not match.", recorded_by="Purchasing"
    )

    assert sent[0][0] == "ledger@example.test"
    assert "PO does not match" in sent[0][1]
    assert "?tab=rejected" in sent[0][1]
