"""The SQLite and PostgreSQL configuration stores must behave the same.

Every other test runs on SQLite, so a difference here means the tests check
behaviour production does not have. Each test runs against SQLite, and against
PostgreSQL when TEST_POSTGRES_URL names a throwaway database (its tables are
emptied before every test; never point it at a real database).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest

from app.approval_matrix import ALL_COMPANIES, ApprovalMatrixStore
from app.companies import CompanyStore
from app.suppliers import SupplierStore
from app.supplier_terms import SupplierTermsStore

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")


@dataclass
class Stores:
    companies: Any
    suppliers: Any
    matrix: Any
    terms: Any


def _sqlite_stores(tmp_path) -> Stores:
    database = tmp_path / "config.db"
    stores = Stores(
        CompanyStore(database),
        SupplierStore(database),
        ApprovalMatrixStore(database),
        SupplierTermsStore(database),
    )
    # The SQLite stores seed demo rows; PostgreSQL starts empty.
    for entry in stores.matrix.list():
        stores.matrix.delete(entry.id)
    for terms in stores.terms.list():
        stores.terms.delete(terms.company, terms.supplier)
    for supplier in stores.suppliers.list():
        stores.suppliers.delete(supplier.name)
    for company in stores.companies.list():
        stores.companies.delete(company.name)
    return stores


def _postgres_stores(monkeypatch) -> Stores:
    import psycopg

    from app.postgres_config import postgres_config_stores
    from app.postgres_invoices import PostgresInvoiceStore
    from app.postgres_settings import PostgresSettings

    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    settings = PostgresSettings.from_env()
    PostgresInvoiceStore(settings, initialize_schema=True)  # applies migrations
    with psycopg.connect(POSTGRES_URL) as connection:
        connection.execute(
            "TRUNCATE companies, suppliers, approval_matrix, supplier_terms "
            "RESTART IDENTITY CASCADE"
        )
    companies, suppliers, matrix, terms, _ = postgres_config_stores(settings)
    return Stores(companies, suppliers, matrix, terms)


@pytest.fixture(scope="module", autouse=True)
def _close_postgres_pools():
    yield
    if POSTGRES_URL:
        from app.postgres_settings import close_postgres_pools

        close_postgres_pools()


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(
                not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set"
            ),
        ),
    ]
)
def stores(request, tmp_path, monkeypatch) -> Stores:
    if request.param == "sqlite":
        return _sqlite_stores(tmp_path)
    return _postgres_stores(monkeypatch)


def _company(stores: Stores, name: str = "Acme Trading Ltd", **extra: Any) -> None:
    stores.companies.create(
        name=name, sharepoint_root_folder=f"Invoices/{name}", **extra
    )


# --- companies ---------------------------------------------------------------


def test_company_lookup_ignores_case_and_accepts_aliases(stores: Stores) -> None:
    _company(stores, aliases=["Acme"])

    assert stores.companies.get("acme trading ltd").name == "Acme Trading Ltd"
    assert stores.companies.get("ACME").name == "Acme Trading Ltd"


def test_company_update_and_delete_ignore_case(stores: Stores) -> None:
    _company(stores)

    stores.companies.update("ACME TRADING LTD", vat_number="GB123")
    assert stores.companies.get("Acme Trading Ltd").vat_number == "GB123"

    stores.companies.delete("acme trading ltd")
    assert stores.companies.get("Acme Trading Ltd") is None


def test_company_names_differing_only_in_case_are_one_company(stores: Stores) -> None:
    _company(stores)

    with pytest.raises(ValueError):
        _company(stores, "ACME TRADING LTD")


def test_deleting_a_missing_company_raises_key_error(stores: Stores) -> None:
    with pytest.raises(KeyError):
        stores.companies.delete("Nobody Ltd")


# --- suppliers ---------------------------------------------------------------


def test_supplier_lookup_and_delete_ignore_case(stores: Stores) -> None:
    stores.suppliers.create(name="Bolt Supplies Ltd", aliases=["Bolt"])

    assert stores.suppliers.get("BOLT SUPPLIES LTD").name == "Bolt Supplies Ltd"
    assert stores.suppliers.get("bolt").name == "Bolt Supplies Ltd"

    stores.suppliers.delete("bolt supplies ltd")
    assert stores.suppliers.get("Bolt Supplies Ltd") is None


def test_supplier_names_differing_only_in_case_are_one_supplier(stores: Stores) -> None:
    stores.suppliers.create(name="Bolt Supplies Ltd")

    with pytest.raises(ValueError):
        stores.suppliers.create(name="BOLT SUPPLIES LTD")


# --- approval matrix ---------------------------------------------------------


def _route(stores: Stores, company: str, supplier: str = "Bolt Supplies Ltd") -> Any:
    return stores.matrix.create(
        company=company,
        supplier=supplier,
        approver1_name="Jordan Blake",
        approver1_email="jordan@example.test",
    )


def test_route_lookup_ignores_case_and_falls_back_to_all_companies(
    stores: Stores,
) -> None:
    _company(stores)
    _company(stores, "Riverside Logistics")
    _route(stores, "Acme Trading Ltd")
    _route(stores, ALL_COMPANIES)

    assert stores.matrix.find("ACME TRADING LTD", "bolt supplies ltd").company == (
        "Acme Trading Ltd"
    )
    assert stores.matrix.find_exact("acme trading ltd", "BOLT SUPPLIES LTD") is not None
    assert stores.matrix.find("Riverside Logistics", "Bolt Supplies Ltd").company == (
        ALL_COMPANIES
    )


def test_route_bulk_deletes_ignore_case(stores: Stores) -> None:
    _company(stores)
    _company(stores, "Riverside Logistics")
    _route(stores, "Acme Trading Ltd")
    _route(stores, "Riverside Logistics", "Other Supplier Ltd")

    stores.matrix.delete_by_company("ACME TRADING LTD")
    stores.matrix.delete_by_supplier("other supplier ltd")

    assert stores.matrix.list() == []


def test_a_second_route_for_the_same_pair_is_refused(stores: Stores) -> None:
    _company(stores)
    _route(stores, "Acme Trading Ltd")

    with pytest.raises(ValueError):
        _route(stores, "ACME TRADING LTD", "BOLT SUPPLIES LTD")


# --- supplier terms ----------------------------------------------------------


def test_terms_upsert_with_different_case_updates_the_same_row(stores: Stores) -> None:
    _company(stores)
    stores.terms.upsert(
        company="Acme Trading Ltd",
        supplier="Bolt Supplies Ltd",
        supplier_account_number="ACC-1",
    )
    stores.terms.upsert(
        company="ACME TRADING LTD",
        supplier="bolt supplies ltd",
        supplier_account_number="ACC-1",
        bank_account="Main",
    )

    rows = stores.terms.list()
    assert len(rows) == 1
    assert rows[0].bank_account == "Main"


def test_terms_lookup_and_deletes_ignore_case(stores: Stores) -> None:
    _company(stores)
    stores.terms.upsert(
        company="Acme Trading Ltd",
        supplier="Bolt Supplies Ltd",
        supplier_account_number="ACC-1",
    )

    assert stores.terms.get("ACME TRADING LTD", "bolt supplies ltd") is not None
    assert len(stores.terms.list_for_supplier("acme trading ltd", "BOLT SUPPLIES LTD")) == 1

    stores.terms.delete_by_supplier("BOLT SUPPLIES LTD")
    assert stores.terms.list() == []
