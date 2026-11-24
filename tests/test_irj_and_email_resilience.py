"""IRJ numbers must not leak on failure; email stages must not be lost on a crash."""

import sqlite3
from pathlib import Path

import pytest

from app.approval_matrix import ApprovalMatrixStore
from app.activity_feed import ActivityFeedStore
from app.companies import CompanyStore
from app.invoice_lifecycle import (
    InvoiceLifecycle,
    InvoiceLifecycleError,
    _is_unique_violation,
)
from app.invoices import InvoiceStore
from app.irj import IrjNumberGenerator
from app.suppliers import SupplierStore


# --- email stage claims ---------------------------------------------------


def test_unsent_stale_claim_is_taken_over_but_sent_claim_never_is(
    tmp_path: Path,
) -> None:
    feed = ActivityFeedStore(tmp_path / "activity.db")

    assert feed.claim_email_stage(1, "po_matching") is True
    # A fresh, unsent claim belongs to the process that made it.
    assert feed.claim_email_stage(1, "po_matching") is False
    # Once the claim is older than the stale window (forced with a negative
    # window) and was never marked sent, another process may take it over.
    assert feed.claim_email_stage(1, "po_matching", stale_after_seconds=-1) is True

    feed.mark_email_stage_sent(1, "po_matching")
    assert feed.claim_email_stage(1, "po_matching", stale_after_seconds=-1) is False


def test_released_claim_can_be_claimed_again(tmp_path: Path) -> None:
    feed = ActivityFeedStore(tmp_path / "activity.db")
    assert feed.claim_email_stage(2, "stage") is True
    feed.release_email_stage(2, "stage")
    assert feed.claim_email_stage(2, "stage") is True


def test_existing_claims_in_an_old_database_count_as_sent(tmp_path: Path) -> None:
    path = tmp_path / "activity.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE invoice_email_stages (
                invoice_id INTEGER NOT NULL,
                stage TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                PRIMARY KEY (invoice_id, stage)
            )
            """
        )
        connection.execute(
            "INSERT INTO invoice_email_stages VALUES (7, 'old_stage', '2020-01-01T00:00:00+00:00')"
        )

    feed = ActivityFeedStore(path)

    # An old row must never be re-sent, however old it is.
    assert feed.claim_email_stage(7, "old_stage", stale_after_seconds=-1) is False


def test_successful_stage_email_is_marked_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, _, invoice_id = _lifecycle(tmp_path)
    monkeypatch.setattr(
        "app.lifecycle.notifications.send_email_notification",
        lambda *, recipient, subject, body: True,
    )

    lifecycle._send_stage_email_once(
        invoice_id, "po_matching", recipient="a@example.test", subject="s", body="b"
    )

    feed = lifecycle.activity_feed
    assert feed.claim_email_stage(invoice_id, "po_matching", stale_after_seconds=-1) is False


def test_failed_send_releases_the_claim_so_it_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, _, invoice_id = _lifecycle(tmp_path)

    def failing_send(*, recipient: str, subject: str, body: str) -> bool:
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr("app.lifecycle.notifications.send_email_notification", failing_send)
    with pytest.raises(RuntimeError):
        lifecycle._send_stage_email_once(
            invoice_id, "po_matching", recipient="a@example.test", subject="s", body="b"
        )

    assert lifecycle.activity_feed.claim_email_stage(invoice_id, "po_matching") is True


# --- IRJ allocation -------------------------------------------------------


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
        approval_matrix_store=ApprovalMatrixStore(tmp_path / "config.db"),
        suppliers_store=SupplierStore(tmp_path / "config.db"),
    )
    return lifecycle, store, record.id


def _confirm(lifecycle: InvoiceLifecycle, invoice_id: int, company: str) -> object:
    return lifecycle.confirm_and_route(
        invoice_id,
        company=company,
        supplier="Supplier Ltd",
        supplier_invoice_number="INV-1",
        purchase_order_number=None,
        invoice_date="2026-08-31",
        invoice_value=100.0,
        currency="GBP",
    )


def _fail_filing_once(
    lifecycle: InvoiceLifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"count": 0}
    original = lifecycle._move_pdf_in_sharepoint

    def flaky(*args: object, **kwargs: object) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise InvoiceLifecycleError("SharePoint filing failed; try again")
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_move_pdf_in_sharepoint", flaky)


def test_failed_filing_does_not_leak_the_irj_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)
    _fail_filing_once(lifecycle, monkeypatch)

    with pytest.raises(InvoiceLifecycleError):
        _confirm(lifecycle, invoice_id, "Acme Trading Ltd")
    # The allocation is recorded on the invoice straight away...
    assert store.get(invoice_id).irj_number == "000001"

    routed = _confirm(lifecycle, invoice_id, "Acme Trading Ltd")

    # ...so the retry reuses it and the sequence has no gap.
    assert routed.irj_number == "000001"
    assert lifecycle.irj_generator.current("Acme Trading Ltd") == "000001"


def test_irj_is_not_reused_when_the_company_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, store, invoice_id = _lifecycle(tmp_path)
    _fail_filing_once(lifecycle, monkeypatch)

    with pytest.raises(InvoiceLifecycleError):
        _confirm(lifecycle, invoice_id, "Acme Trading Ltd")
    routed = _confirm(lifecycle, invoice_id, "Northfield Manufacturing")

    # Numbers are per company, so the new company gets its own first number.
    assert routed.irj_number == "000001"
    assert routed.company == "Northfield Manufacturing"
    assert lifecycle.irj_generator.current("Northfield Manufacturing") == "000001"


def test_unique_violations_are_recognised_from_both_databases() -> None:
    class UniqueViolation(Exception):
        """Stands in for psycopg.errors.UniqueViolation."""

    assert _is_unique_violation(sqlite3.IntegrityError("UNIQUE constraint failed"))
    assert _is_unique_violation(UniqueViolation())
    assert not _is_unique_violation(ValueError("other"))
