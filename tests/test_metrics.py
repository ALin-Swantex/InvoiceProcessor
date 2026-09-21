from datetime import date

from app.invoices import InvoiceRecord
from app.metrics import build_metrics
from app.supplier_terms import SupplierTerms


def invoice(
    invoice_id: int,
    *,
    company: str = "Acme",
    supplier: str = "Supplier Ltd",
    status: str = "Awaiting Sage Registration",
    invoice_date: str = "2026-09-01",
    created_at: str = "2026-09-01T10:00:00+00:00",
    value: float = 100.0,
    currency: str = "GBP",
    document_type: str = "invoice",
) -> InvoiceRecord:
    return InvoiceRecord(
        id=invoice_id,
        message_id=f"message-{invoice_id}",
        attachment_id=f"attachment-{invoice_id}",
        internet_message_id=None,
        sender_name=None,
        sender_address=None,
        subject=None,
        received_at=created_at,
        original_filename=f"invoice-{invoice_id}.pdf",
        stored_path=f"/tmp/invoice-{invoice_id}.pdf",
        size_bytes=100,
        status=status,
        created_at=created_at,
        company=company,
        supplier=supplier,
        invoice_date=invoice_date,
        invoice_value=value,
        currency=currency,
        document_type=document_type,
    )


def terms(notice: str) -> SupplierTerms:
    return SupplierTerms(
        id=1,
        company="Acme",
        supplier="Supplier Ltd",
        payment_terms_notice=notice,
    )


def test_metrics_group_values_without_combining_currencies() -> None:
    metrics = build_metrics(
        [
            invoice(1, value=100, currency="GBP"),
            invoice(2, value=50, currency="USD"),
            invoice(3, company="Beta", value=25, currency="GBP"),
            invoice(4, value=999, status="Rejected"),
        ],
        [terms("30 Days")],
        granularity="month",
        today=date(2026, 9, 14),
    )

    assert metrics["pending_by_company"] == [
        {"name": "Acme", "currency": "GBP", "count": 1, "value": 100.0},
        {"name": "Acme", "currency": "USD", "count": 1, "value": 50.0},
        {"name": "Beta", "currency": "GBP", "count": 1, "value": 25.0},
    ]
    assert len(metrics["spend_by_supplier_company"]) == 3


def test_due_dates_support_days_eom_and_report_unknown_terms() -> None:
    metrics = build_metrics(
        [
            invoice(1, invoice_date="2026-08-01"),
            invoice(2, invoice_date="2026-09-01"),
        ],
        [terms("30 Days EOM")],
        granularity="day",
        today=date(2026, 9, 25),
    )

    due = metrics["due_dates"]
    assert due["approaching"][0]["invoice_id"] == 1
    assert due["approaching"][0]["due_date"] == "2026-09-30"
    assert due["unavailable_count"] == 0

    unknown = build_metrics(
        [invoice(3)],
        [terms("Special Terms")],
        granularity="week",
        today=date(2026, 9, 14),
    )
    assert unknown["due_dates"]["unavailable_count"] == 1


def test_volume_uses_selected_granularity() -> None:
    metrics = build_metrics(
        [
            invoice(1, created_at="2026-09-14T08:00:00+00:00"),
            invoice(2, created_at="2026-09-13T08:00:00+00:00"),
        ],
        [],
        granularity="day",
        today=date(2026, 9, 14),
    )

    assert metrics["volume"][-1] == {"period": "14 Sep", "count": 1}
    assert metrics["volume"][-2] == {"period": "13 Sep", "count": 1}


def test_volume_excludes_supplier_statements() -> None:
    metrics = build_metrics(
        [
            invoice(1),
            invoice(2, document_type="statement"),
        ],
        [],
        granularity="month",
        today=date(2026, 9, 14),
    )

    assert metrics["volume"][-1]["count"] == 1
