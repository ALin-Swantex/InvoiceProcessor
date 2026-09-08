from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.postgres_invoices import PostgresInvoiceStore
from app.postgres_services import PostgresIrjNumberGenerator
from app.postgres_settings import AAD_SCOPE, PostgresSettings


class Token:
    token = "entra-token"


class Credential:
    def __init__(self) -> None:
        self.scopes: list[str] = []

    def get_token(self, scope: str) -> Token:
        self.scopes.append(scope)
        return Token()


def test_explicit_settings_require_host_database_and_user() -> None:
    with pytest.raises(ValueError, match="AZURE_POSTGRES_DATABASE"):
        PostgresSettings.from_env(
            {"AZURE_POSTGRES_HOST": "server.postgres.database.azure.com"}
        )


def test_database_url_parses_encoded_credentials_and_sslmode() -> None:
    settings = PostgresSettings.from_env(
        {
            "DATABASE_URL": (
                "postgresql://user%40tenant:p%40ss@db.example:6432/invoices"
                "?sslmode=verify-full"
            )
        }
    )
    assert settings == PostgresSettings(
        host="db.example",
        database="invoices",
        user="user@tenant",
        password="p@ss",
        port=6432,
        sslmode="verify-full",
        sslrootcert=None,
    )


def test_missing_password_uses_entra_token_as_connection_password() -> None:
    credential = Credential()
    kwargs = PostgresSettings("db.example", "invoices", "user").connection_kwargs(
        credential
    )
    assert kwargs["password"] == "entra-token"
    assert kwargs["dbname"] == "invoices"
    assert credential.scopes == [AAD_SCOPE]


def test_optional_dependencies_are_not_imported_by_module_import() -> None:
    assert "psycopg" not in sys.modules
    assert "azure.identity" not in sys.modules


class FakeCursor:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self.row = row
        self.calls: list[tuple[str, object | None]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, parameters: object | None = None) -> None:
        self.calls.append((sql, parameters))

    def fetchone(self) -> dict[str, object] | None:
        return self.row

    def fetchall(self) -> list[dict[str, object]]:
        return [self.row] if self.row is not None else []


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.exited_with: type[BaseException] | None = None

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> None:
        self.exited_with = exception_type

    def cursor(self) -> FakeCursor:
        return self._cursor


def test_update_fields_rejects_unknown_identifier_without_connecting() -> None:
    connected = False

    def connect() -> FakeConnection:
        nonlocal connected
        connected = True
        return FakeConnection(FakeCursor())

    store = PostgresInvoiceStore(
        connection_factory=connect, initialize_schema=False
    )
    with pytest.raises(ValueError, match="malicious"):
        store.update_fields(1, **{"malicious = NULL; DROP TABLE invoices": "x"})
    assert connected is False


def test_update_fields_binds_values() -> None:
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    store = PostgresInvoiceStore(
        connection_factory=lambda: connection, initialize_schema=False
    )

    with pytest.raises(KeyError):
        store.update_fields(42, status="Approved'; DROP TABLE invoices; --")

    sql, parameters = cursor.calls[0]
    assert "status = %s" in sql
    assert "DROP TABLE" not in sql
    assert parameters == ("Approved'; DROP TABLE invoices; --", 42)
    assert connection.exited_with is None


def test_add_from_outlook_uses_atomic_conflict_handling() -> None:
    cursor = FakeCursor()
    store = PostgresInvoiceStore(
        connection_factory=lambda: FakeConnection(cursor), initialize_schema=False
    )
    with pytest.raises(RuntimeError):
        store.add_from_outlook(
            message={"id": "message-1"},
            attachment={"id": "attachment-1", "name": "invoice.pdf"},
            stored_path=Path("outlook_downloads/invoice.pdf"),
        )
    sql, parameters = cursor.calls[0]
    assert "ON CONFLICT (message_id, attachment_id)" in sql
    assert "%s" in sql
    assert parameters[0:2] == ("message-1", "attachment-1")


def test_postgres_irj_generation_is_atomic() -> None:
    cursor = FakeCursor({"number": 42})
    generator = PostgresIrjNumberGenerator(
        connection_factory=lambda: FakeConnection(cursor)
    )

    assert generator.generate() == "IRJ-000042"

    sql, parameters = cursor.calls[0]
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "RETURNING next_number - 1" in sql
    assert parameters is None


def test_authoritative_schema_contains_current_and_company_scoped_tables() -> None:
    schema = (
        Path(__file__).parents[1]
        / "app"
        / "postgres_migrations"
        / "001_authoritative_schema.sql"
    ).read_text()

    assert "CREATE TABLE invoices" in schema
    assert "ai_field_confidences text" in schema
    assert "CREATE TABLE company_user_access" in schema
    assert "CREATE TABLE approval_matrix" in schema
    assert "CREATE TABLE supplier_terms" in schema
    assert "ENABLE ROW LEVEL SECURITY" not in schema


def test_insecure_remote_postgres_sslmode_is_rejected() -> None:
    with pytest.raises(ValueError, match="sslmode"):
        PostgresSettings(
            "db.example",
            "invoices",
            "user",
            sslmode="disable",
        )


def test_local_postgres_can_disable_transport_encryption() -> None:
    settings = PostgresSettings.from_env(
        {
            "DATABASE_URL": (
                "postgresql://invoice_processor:local-password"
                "@127.0.0.1:5432/invoice_processing?sslmode=disable"
            )
        }
    )

    assert settings.host == "127.0.0.1"
    assert settings.sslmode == "disable"
    assert settings.connection_kwargs()["password"] == "local-password"
