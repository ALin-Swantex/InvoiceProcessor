from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

from app.invoices import InvoiceRecord
from app.supplier_terms import SupplierTerms


EXCLUDED_FINANCIAL_STATUSES = {
    "Cancelled - Duplicate",
    "Rejected",
    "Reconciled / Complete",
    "Statement Filed",
}
PAID_STATUSES = {
    "Paid / Awaiting Bank Reconciliation",
    "Reconciled / Complete",
}


def build_metrics(
    invoices: Iterable[InvoiceRecord],
    supplier_terms: Iterable[SupplierTerms],
    *,
    granularity: str,
    today: date | None = None,
) -> dict[str, object]:
    if granularity not in {"day", "week", "month"}:
        raise ValueError("Granularity must be day, week, or month.")
    today = today or date.today()
    invoice_rows = list(invoices)
    terms_rows = list(supplier_terms)

    invoice_documents = [
        invoice for invoice in invoice_rows if invoice.document_type == "invoice"
    ]
    volume = _invoice_volume(invoice_documents, granularity, today)
    pending = [
        invoice
        for invoice in invoice_rows
        if invoice.document_type == "invoice"
        and invoice.status not in EXCLUDED_FINANCIAL_STATUSES | PAID_STATUSES
    ]
    valid_spend = [
        invoice
        for invoice in invoice_rows
        if invoice.document_type == "invoice"
        and invoice.status not in EXCLUDED_FINANCIAL_STATUSES
    ]

    return {
        "granularity": granularity,
        "volume": volume,
        "due_dates": _due_date_metrics(pending, terms_rows, today),
        "pending_by_company": _group_values(
            pending, lambda invoice: invoice.company or "Unassigned"
        ),
        "spend_by_supplier_company": _group_values(
            valid_spend,
            lambda invoice: (
                f"{invoice.company or 'Unassigned'} — "
                f"{invoice.supplier or 'Unassigned'}"
            ),
        ),
    }


def _invoice_volume(
    invoices: list[InvoiceRecord], granularity: str, today: date
) -> list[dict[str, object]]:
    bucket_count = {"day": 30, "week": 12, "month": 12}[granularity]
    current = _bucket_start(today, granularity)
    starts = [_shift_bucket(current, granularity, offset) for offset in range(1 - bucket_count, 1)]
    counts = {start: 0 for start in starts}
    for invoice in invoices:
        created = _parse_date(invoice.created_at)
        if created is None:
            continue
        bucket = _bucket_start(created, granularity)
        if bucket in counts:
            counts[bucket] += 1
    return [
        {"period": _bucket_label(start, granularity), "count": counts[start]}
        for start in starts
    ]


def _due_date_metrics(
    invoices: list[InvoiceRecord],
    terms_rows: list[SupplierTerms],
    today: date,
) -> dict[str, object]:
    overdue: list[dict[str, object]] = []
    approaching: list[dict[str, object]] = []
    unavailable = 0
    terms_by_supplier: dict[tuple[str, str], list[SupplierTerms]] = defaultdict(list)
    for terms in terms_rows:
        terms_by_supplier[
            (
                terms.company.strip().casefold(),
                terms.supplier.strip().casefold(),
            )
        ].append(terms)
    for invoice in invoices:
        invoice_date = _parse_date(invoice.invoice_date)
        terms = _matching_terms(invoice, terms_by_supplier)
        due_date = (
            _calculate_due_date(invoice_date, terms.payment_terms_notice)
            if invoice_date is not None and terms is not None
            else None
        )
        if due_date is None:
            unavailable += 1
            continue
        days_remaining = (due_date - today).days
        row = {
            "invoice_id": invoice.id,
            "irj_number": invoice.irj_number,
            "company": invoice.company or "Unassigned",
            "supplier": invoice.supplier or "Unassigned",
            "due_date": due_date.isoformat(),
            "days_remaining": days_remaining,
            "value": invoice.invoice_value,
            "currency": invoice.currency or "GBP",
        }
        if days_remaining < 0:
            overdue.append(row)
        elif days_remaining <= 7:
            approaching.append(row)
    overdue.sort(key=lambda row: int(row["days_remaining"]))
    approaching.sort(key=lambda row: int(row["days_remaining"]))
    return {
        "overdue": overdue,
        "approaching": approaching,
        "unavailable_count": unavailable,
    }


def _matching_terms(
    invoice: InvoiceRecord,
    terms_by_supplier: dict[tuple[str, str], list[SupplierTerms]],
) -> SupplierTerms | None:
    company = (invoice.company or "").strip().casefold()
    supplier = (invoice.supplier or "").strip().casefold()
    matches = terms_by_supplier.get((company, supplier), [])
    account = (invoice.supplier_account_number or "").strip().casefold()
    if account:
        for terms in matches:
            if (terms.supplier_account_number or "").strip().casefold() == account:
                return terms
    return matches[0] if matches else None


def _calculate_due_date(
    invoice_date: date, payment_terms: str | None
) -> date | None:
    terms = (payment_terms or "").strip().casefold()
    if terms in {"due on receipt", "on demand"}:
        return invoice_date
    if "bol" in terms or "special" in terms or "deposit" in terms:
        return None
    match = re.search(r"\b(\d+)\s*days?\b", terms)
    if match is None:
        return None
    days = int(match.group(1))
    if "eom" in terms:
        month_end = invoice_date.replace(
            day=calendar.monthrange(invoice_date.year, invoice_date.month)[1]
        )
        return month_end + timedelta(days=days)
    return invoice_date + timedelta(days=days)


def _group_values(
    invoices: list[InvoiceRecord], key
) -> list[dict[str, object]]:
    totals: dict[tuple[str, str], dict[str, object]] = defaultdict(
        lambda: {"count": 0, "value": 0.0}
    )
    for invoice in invoices:
        if invoice.invoice_value is None:
            continue
        currency = (invoice.currency or "GBP").upper()
        group = totals[(key(invoice), currency)]
        group["count"] = int(group["count"]) + 1
        group["value"] = float(group["value"]) + float(invoice.invoice_value)
    return [
        {
            "name": name,
            "currency": currency,
            "count": values["count"],
            "value": round(float(values["value"]), 2),
        }
        for (name, currency), values in sorted(
            totals.items(), key=lambda item: float(item[1]["value"]), reverse=True
        )
    ]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def _bucket_start(value: date, granularity: str) -> date:
    if granularity == "day":
        return value
    if granularity == "week":
        return value - timedelta(days=value.weekday())
    return value.replace(day=1)


def _shift_bucket(value: date, granularity: str, offset: int) -> date:
    if granularity == "day":
        return value + timedelta(days=offset)
    if granularity == "week":
        return value + timedelta(weeks=offset)
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def _bucket_label(value: date, granularity: str) -> str:
    if granularity == "month":
        return value.strftime("%b %Y")
    if granularity == "week":
        return f"Week of {value.strftime('%d %b')}"
    return value.strftime("%d %b")
