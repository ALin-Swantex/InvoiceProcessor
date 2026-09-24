from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest

from app.postgres_invoices import PostgresInvoiceStore
from app.postgres_services import PostgresIrjNumberGenerator
from app import postgres_settings
from app.postgres_settings import AAD_SCOPE, PostgresSettings


class Token:
    token = "entra-token"
    expires_on = 9999999999


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


def test_password_connection_factory_uses_checked_shared_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []

    class FakePool:
        @staticmethod
        def check_connection(connection: object) -> None:
            del connection

        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def connection(self) -> object:
            return object()

    class FakePoolModule:
        ConnectionPool = FakePool

    real_import_module = postgres_settings.importlib.import_module

    def import_module(name: str) -> object:
        if name == "psycopg_pool":
            return FakePoolModule
        return real_import_module(name)

    monkeypatch.setattr(postgres_settings.importlib, "import_module", import_module)
    postgres_settings._postgres_pool.cache_clear()
    settings = PostgresSettings(
        "127.0.0.1",
        "invoices",
        "user",
        password="-".join(("pool", "credential")),
        sslmode="disable",
    )

    first = postgres_settings.postgres_connection_factory(settings)
    second = postgres_settings.postgres_connection_factory(settings)

    assert len(created) == 1
    assert first.__self__ is second.__self__
    assert created[0]["check"] is FakePool.check_connection
    assert created[0]["max_idle"] == 300.0
    postgres_settings._postgres_pool.cache_clear()


def test_entra_connection_factory_uses_token_aware_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []

    class FakeConnection:
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs: object) -> object:
            return (conninfo, kwargs)

    class FakePool:
        @staticmethod
        def check_connection(connection: object) -> None:
            del connection

        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        def connection(self) -> object:
            return object()

    class FakePoolModule:
        ConnectionPool = FakePool

    class FakePsycopg:
        Connection = FakeConnection

    class FakeRows:
        dict_row = object()

    credential = Credential()
    monkeypatch.setattr(
        postgres_settings,
        "_default_credential",
        lambda *args: credential,
    )
    real_import_module = postgres_settings.importlib.import_module

    def import_module(name: str) -> object:
        if name == "psycopg_pool":
            return FakePoolModule
        if name == "psycopg":
            return FakePsycopg
        if name == "psycopg.rows":
            return FakeRows
        return real_import_module(name)

    monkeypatch.setattr(postgres_settings.importlib, "import_module", import_module)
    postgres_settings._entra_postgres_pool.cache_clear()
    settings = PostgresSettings("db.example", "invoices", "Invoice MCP")

    first = postgres_settings.postgres_connection_factory(settings)
    second = postgres_settings.postgres_connection_factory(settings)

    assert len(created) == 1
    assert first.__self__ is second.__self__
    assert created[0]["connection_class"].__name__ == "EntraConnection"
    assert created[0]["max_lifetime"] == 2700
    assert "password" not in created[0]["kwargs"]
    postgres_settings._entra_postgres_pool.cache_clear()


def test_optional_dependencies_are_not_imported_by_module_import() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.postgres_settings; "
                "assert 'psycopg' not in sys.modules; "
                "assert 'azure.identity' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


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


def test_update_fields_if_status_uses_atomic_status_guard() -> None:
    cursor = FakeCursor()
    store = PostgresInvoiceStore(
        connection_factory=lambda: FakeConnection(cursor), initialize_schema=False
    )

    assert (
        store.update_fields_if_status(
            42,
            "Awaiting Approval 2",
            status="Approved",
        )
        is None
    )

    sql, parameters = cursor.calls[0]
    assert "WHERE id = %s AND status = %s" in sql
    assert parameters == ("Approved", 42, "Awaiting Approval 2")


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

    assert generator.generate() == "000042"

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
    assert "CREATE TABLE approval_matrix" in schema
    assert "CREATE TABLE supplier_terms" in schema
    assert "ENABLE ROW LEVEL SECURITY" not in schema

    user_removal = (
        Path(__file__).parents[1]
        / "app"
        / "postgres_migrations"
        / "010_remove_users_add_email_stages.sql"
    ).read_text()
    assert "DROP TABLE IF EXISTS company_user_access" in user_removal
    assert "DROP TABLE IF EXISTS user_sessions" in user_removal
    assert "DROP TABLE IF EXISTS users" in user_removal
    assert "CREATE TABLE invoice_email_stages" in user_removal

    operations = (
        Path(__file__).parents[1]
        / "app"
        / "postgres_migrations"
        / "013_operational_visibility.sql"
    ).read_text()
    assert "CREATE TABLE worker_heartbeats" in operations
    assert "CREATE OR REPLACE VIEW bi_invoice_metadata" in operations
    assert "CREATE VIEW bi_invoice_corrections" in operations
    assert "corrected_fields_json" in operations


def test_company_irj_migration_allows_same_irj_across_companies() -> None:
    migration = (
        Path(__file__).parents[1]
        / "app"
        / "postgres_migrations"
        / "007_company_irj_sequences.sql"
    ).read_text()

    assert "CREATE TABLE company_irj_sequences" in migration
    assert "DROP CONSTRAINT IF EXISTS invoices_irj_number_key" in migration
    assert "lower(company), irj_number" in migration


def test_company_irj_seed_migration_continues_existing_sequences() -> None:
    migration = (
        Path(__file__).parents[1]
        / "app"
        / "postgres_migrations"
        / "008_seed_company_irj_sequences.sql"
    ).read_text()

    assert "max(irj_number::bigint) + 1" in migration
    assert "greatest(sequence.next_number, maxima.next_number)" in migration
    assert "WHERE NOT EXISTS" in migration


def test_supplier_company_link_migration_backfills_foreign_keys() -> None:
    migration = (
        Path(__file__).parents[1]
        / "app"
        / "postgres_migrations"
        / "009_link_suppliers_to_companies.sql"
    ).read_text()

    assert "SET default_company_id = company.id" in migration
    assert "lower(company.name) = lower(supplier.default_company)" in migration
    assert "idx_suppliers_default_company_id" in migration


def test_document_classification_migration_adds_statement_fields() -> None:
    migration = (
        Path(__file__).parents[1]
        / "app"
        / "postgres_migrations"
        / "002_document_classification.sql"
    ).read_text()

    assert "document_type text NOT NULL DEFAULT 'invoice'" in migration
    assert "document_classification_confidence" in migration
    assert "document_classification_reason" in migration


def test_irj_format_migration_removes_legacy_prefix() -> None:
    migration = (
        Path(__file__).parents[1]
        / "app"
        / "postgres_migrations"
        / "003_six_digit_irj_numbers.sql"
    ).read_text()

    assert "SET irj_number = substring(irj_number FROM 5)" in migration
    assert "^[0-9]{6}$" in migration


def test_supplier_pattern_migration_adds_configuration_field() -> None:
    migration = (
        Path(__file__).parents[1]
        / "app"
        / "postgres_migrations"
        / "004_supplier_invoice_number_patterns.sql"
    ).read_text()

    assert "ADD COLUMN invoice_number_pattern text" in migration


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
