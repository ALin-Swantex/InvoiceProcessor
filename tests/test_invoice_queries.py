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


def test_workqueue_keeps_old_unfinished_invoices_but_caps_finished_ones(
    tmp_path: Path,
) -> None:
    store = InvoiceStore(tmp_path / "i.db")
    old_open = _add(store, 0, tmp_path, status="Awaiting Approval 2",
                    approver2_email="a@x.com", created_at="2020-01-01T00:00:00+00:00")
    done = [
        _add(store, n, tmp_path, status="Reconciled / Complete",
             created_at=f"2025-01-0{n}T00:00:00+00:00")
        for n in range(1, 5)
    ]

    queue = {invoice.id for invoice in store.list_workqueue(2)}

    assert queue == {old_open, done[-1], done[-2]}
    assert {i.id for i in store.list_workqueue(2, (2, " A@x.com "))} == {old_open}
    assert store.list_workqueue(2, (1, "a@x.com")) == []


def test_search_matches_trimmed_case_insensitive_and_scopes_to_approver(
    tmp_path: Path,
) -> None:
    store = InvoiceStore(tmp_path / "i.db")
    mine = _add(store, 1, tmp_path, company="Acme", supplier="Bolt Ltd",
                irj_number="000007", approver1_email="a@x.com")
    _add(store, 2, tmp_path, company="Acme", supplier="Bolt Ltd",
         approver1_email="other@x.com")

    assert len(store.search(company=" acme ", supplier="BOLT LTD")) == 2
    assert [i.id for i in store.search(irj_number="000007")] == [mine]
    assert [i.id for i in store.search(company="acme", approver=(1, "A@x.com"))] == [mine]
    assert store.search() == []


def test_duplicate_checks_look_beyond_the_newest_500_invoices(tmp_path: Path) -> None:
    from app.duplicates import find_possible_duplicate

    store = InvoiceStore(tmp_path / "i.db")
    first = _add(store, 0, tmp_path, company="Acme", supplier="Bolt Ltd",
                 supplier_invoice_number="INV-9", created_at="2019-01-01T00:00:00+00:00")
    for n in range(1, 520):
        _add(store, n, tmp_path, company="Acme", supplier="Other",
             created_at=f"2024-01-01T00:00:{n % 60:02d}+00:00")
    new = _add(store, 900, tmp_path, company="Acme", supplier="Bolt Ltd")

    match = find_possible_duplicate(
        store, exclude_invoice_id=new, company="acme", supplier="BOLT LTD ",
        supplier_invoice_number=" inv-9",
    )

    assert match is not None and match.invoice_id == first
    assert match.match_basis == "supplier invoice number"
