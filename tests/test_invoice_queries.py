"""Targeted invoice queries that replace full-table scans."""

from pathlib import Path

from app.invoices import InvoiceStore


def _add(store: InvoiceStore, n: int, tmp_path: Path, **fields: object) -> int:
    record = store.add_from_outlook(
        message={"id": f"m{n}"},
        attachment={"id": f"a{n}", "name": f"inv{n}.pdf"},
        stored_path=tmp_path / f"inv{n}.pdf",
    )
    if fields:
        store.update_fields(record.id, **fields)
    return record.id


def test_list_all_with_status_returns_only_matching_rows_without_a_limit(
    tmp_path: Path,
) -> None:
    store = InvoiceStore(tmp_path / "i.db")
    wanted = [
        _add(store, n, tmp_path, status="Awaiting Approval 1") for n in range(3)
    ]
    _add(store, 10, tmp_path, status="Paid")
    _add(store, 11, tmp_path, status="Rejected")

    found = store.list_all_with_status(["Awaiting Approval 1", "Rejected"])

    assert len(found) == 4
    assert set(wanted) <= {invoice.id for invoice in found}
    assert store.list_all_with_status([]) == []


def test_highest_irj_number_ignores_other_companies_and_non_numeric(
    tmp_path: Path,
) -> None:
    store = InvoiceStore(tmp_path / "i.db")
    _add(store, 1, tmp_path, company="Acme", irj_number="000012")
    _add(store, 2, tmp_path, company="acme", irj_number="000040")
    _add(store, 3, tmp_path, company="Acme", irj_number="LEGACY-9")
    _add(store, 4, tmp_path, company="Beta", irj_number="000999")

    assert store.highest_irj_number("ACME") == 40
    assert store.highest_irj_number("Gamma") is None
