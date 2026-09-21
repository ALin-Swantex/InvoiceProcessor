from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.postgres_config import (
    PostgresApprovalMatrixStore,
    PostgresCompanyStore,
    PostgresProcessConfigurationStore,
    PostgresSupplierStore,
    PostgresSupplierTermsStore,
)
from app.postgres_config_migrate import migrate_sqlite_configuration
from app.config_db import configuration_backend


class RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object | None]] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, parameters: object | None = None) -> None:
        self.calls.append((sql, parameters))

    def executemany(self, sql: str, parameters: object) -> None:
        self.calls.append((sql, parameters))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class RecordingConnection:
    def __init__(self) -> None:
        self.recording_cursor = RecordingCursor()
        self.exited_with = None

    def __enter__(self):
        return self

    def __exit__(self, exception_type, *args: object) -> None:
        self.exited_with = exception_type

    def cursor(self) -> RecordingCursor:
        return self.recording_cursor


def _source_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE companies (
            name TEXT, sharepoint_root_folder TEXT, company_folder TEXT,
            po_matching_folder TEXT, aliases TEXT, vat_number TEXT, address TEXT
        );
        CREATE TABLE suppliers (
            name TEXT, aliases TEXT, default_company TEXT, contact_email TEXT,
            invoice_number_pattern TEXT
        );
        CREATE TABLE approval_matrix (
            company TEXT, supplier TEXT, approver1_name TEXT,
            approver1_email TEXT, approver2_name TEXT, approver2_email TEXT
        );
        CREATE TABLE supplier_terms (
            company TEXT, supplier TEXT, supplier_account_number TEXT,
            default_payment_method TEXT, payment_terms_notice TEXT,
            bank_account TEXT
        );
        CREATE TABLE process_configuration (key TEXT, value TEXT);
        INSERT INTO companies VALUES (
            'SWAN', 'Invoices/SWAN', 'Invoices/SWAN/Nominal Invoices',
            'Invoices/SWAN/PO Invoices/PO Match', 'Swantex', NULL, NULL
        );
        INSERT INTO suppliers VALUES (
            'Supplier Ltd', 'Supplier Limited', 'SWAN', 'a@example.test', 'INV-####'
        );
        INSERT INTO approval_matrix VALUES (
            'SWAN', 'Supplier Ltd', 'One', 'one@example.test', NULL, NULL
        );
        INSERT INTO supplier_terms VALUES (
            'SWAN', 'Supplier Ltd', 'A001', 'BACS', '30 days', 'Main'
        );
        INSERT INTO supplier_terms VALUES (
            '*', 'Supplier Ltd', NULL, 'BACS', 'Default terms', 'Main'
        );
        INSERT INTO process_configuration VALUES ('ai_confidence_threshold', '0.8');
        """
    )
    connection.commit()
    connection.close()


def test_sqlite_configuration_import_copies_every_table(tmp_path: Path) -> None:
    source = tmp_path / "config.db"
    _source_database(source)
    destination = RecordingConnection()

    counts = migrate_sqlite_configuration(
        source, connection_factory=lambda: destination
    )

    assert counts == {
        "companies": 1,
        "suppliers": 1,
        "approval_matrix": 1,
        "supplier_terms": 2,
        "process_configuration": 1,
    }
    sql = "\n".join(call[0] for call in destination.recording_cursor.calls)
    assert "ON CONFLICT (lower(name))" in sql
    assert "ON CONFLICT (lower(company), lower(supplier))" in sql
    assert "ON CONFLICT (company, supplier_account_number)" in sql
    assert (
        "ON CONFLICT (company, supplier) "
        "WHERE supplier_account_number IS NULL"
    ) in " ".join(sql.split())
    assert destination.exited_with is None


def test_configuration_import_requires_existing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        migrate_sqlite_configuration(tmp_path / "missing.db")


def test_sharepoint_list_invoice_backend_keeps_sqlite_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONFIG_STORE_BACKEND", raising=False)
    assert configuration_backend("sharepoint_list") == "sqlite"


def test_explicit_configuration_backend_overrides_invoice_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONFIG_STORE_BACKEND", "sqlite")
    assert configuration_backend("postgres") == "sqlite"


def test_postgres_process_configuration_uses_parameterized_upsert() -> None:
    connection = RecordingConnection()
    store = PostgresProcessConfigurationStore(
        connection_factory=lambda: connection
    )

    store.set("ai_confidence_threshold", "0.75")

    sql, parameters = connection.recording_cursor.calls[0]
    assert "ON CONFLICT (key) DO UPDATE" in sql
    assert parameters == ("ai_confidence_threshold", "0.75")


def test_supplier_terms_null_account_parameter_has_explicit_type() -> None:
    connection = RecordingConnection()
    store = PostgresSupplierTermsStore(connection_factory=lambda: connection)

    with pytest.raises(RuntimeError, match="did not return supplier terms ID"):
        store.upsert(
            company="GIFTED",
            supplier="Supplier Ltd",
            supplier_account_number=None,
        )

    sql, parameters = connection.recording_cursor.calls[0]
    assert "%s::text IS NULL" in sql
    assert parameters == ("GIFTED", None, None, "Supplier Ltd")


def test_postgres_bulk_import_uses_set_based_upserts_in_one_connection() -> None:
    connection = RecordingConnection()
    store = PostgresSupplierStore(connection_factory=lambda: connection)

    store.bulk_import_master_data(
        suppliers=[("Supplier One", "GIFTED"), ("Supplier Two", "GIFTED")],
        approvals=[
            (
                "GIFTED",
                "Supplier One",
                "Approver",
                "approver@example.test",
                None,
                None,
            )
        ],
        terms=[
            ("GIFTED", "Supplier One", "A001", "BACS", "30 days", "Main"),
            ("GIFTED", "Supplier Two", None, None, None, None),
        ],
    )

    assert len(connection.recording_cursor.calls) == 4
    sql = "\n".join(call[0] for call in connection.recording_cursor.calls)
    assert "ON CONFLICT (lower(name)) DO UPDATE" in sql
    assert "ON CONFLICT (lower(company), lower(supplier)) DO UPDATE" in sql
    assert "ON CONFLICT (company, supplier_account_number) DO UPDATE" in sql
    assert "WHERE supplier_account_number IS NULL" in sql
    assert sql.count("jsonb_to_recordset") == 4


@pytest.mark.parametrize(
    ("store_type", "method", "message"),
    [
        (PostgresCompanyStore, "update", "Unknown company fields"),
        (PostgresSupplierStore, "update", "Supplier 'x' was not found"),
        (
            PostgresApprovalMatrixStore,
            "update",
            "Unknown approval matrix fields",
        ),
    ],
)
def test_postgres_stores_reject_invalid_updates(
    store_type, method: str, message: str
) -> None:
    connection = RecordingConnection()
    store = store_type(connection_factory=lambda: connection)
    with pytest.raises((ValueError, KeyError), match=message):
        getattr(store, method)(1 if store_type is PostgresApprovalMatrixStore else "x", malicious="value")
