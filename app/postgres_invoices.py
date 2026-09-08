from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from app.invoices import InvoiceRecord
from app.postgres_settings import (
    ConnectionFactory,
    PostgresSettings,
    postgres_connection_factory,
)


MIGRATIONS_DIRECTORY = Path(__file__).with_name("postgres_migrations")
_RECORD_COLUMNS = tuple(InvoiceRecord.__dataclass_fields__)
_UPDATABLE_COLUMNS = frozenset(_RECORD_COLUMNS) - {"id"}
_SELECT_COLUMNS = ", ".join(_RECORD_COLUMNS)


class PostgresInvoiceStore:
    """PostgreSQL implementation of the current InvoiceStore contract."""

    def __init__(
        self,
        settings: PostgresSettings | None = None,
        *,
        connection_factory: ConnectionFactory | None = None,
        initialize_schema: bool = True,
        migrations_directory: Path = MIGRATIONS_DIRECTORY,
    ) -> None:
        if connection_factory is None:
            connection_factory = postgres_connection_factory(settings)
        self._connection_factory = connection_factory
        self._migrations_directory = migrations_directory
        if initialize_schema:
            self.initialize_schema()

    def initialize_schema(self) -> None:
        migrations = sorted(self._migrations_directory.glob("*.sql"))
        if not migrations:
            raise RuntimeError("No PostgreSQL schema migrations were found.")
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                # Serialize startup so two workers cannot apply the same migration.
                cursor.execute("SELECT pg_advisory_xact_lock(734921684)")
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version text PRIMARY KEY,
                        checksum text NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                cursor.execute("SELECT version, checksum FROM schema_migrations")
                applied = {row["version"]: row["checksum"] for row in cursor.fetchall()}
                for migration in migrations:
                    sql = migration.read_text(encoding="utf-8")
                    normalized_sql = sql.replace("\r\n", "\n").replace("\r", "\n")
                    checksum = hashlib.sha256(normalized_sql.encode()).hexdigest()
                    previous = applied.get(migration.name)
                    if previous and previous != checksum:
                        raise RuntimeError(
                            f"Applied migration {migration.name} has changed."
                        )
                    if previous:
                        continue
                    cursor.execute(sql)
                    cursor.execute(
                        """
                        INSERT INTO schema_migrations (version, checksum)
                        VALUES (%s, %s)
                        """,
                        (migration.name, checksum),
                    )

    def add_from_outlook(
        self,
        *,
        message: dict[str, object],
        attachment: dict[str, object],
        stored_path: Path,
    ) -> InvoiceRecord:
        message_id = self._required_string(message, "id")
        attachment_id = self._required_string(attachment, "id")
        filename = self._required_string(attachment, "name")
        sender_name, sender_address = self._sender(message)
        sharepoint_item_id = self._optional_string(
            attachment.get("sharepoint_item_id")
        )
        sharepoint_web_url = self._optional_string(
            attachment.get("sharepoint_web_url")
        )
        values = (
            message_id,
            attachment_id,
            self._optional_string(message.get("internetMessageId")),
            sender_name,
            sender_address,
            self._optional_string(message.get("subject")),
            self._optional_string(message.get("receivedDateTime")),
            filename,
            str(stored_path),
            self._optional_int(attachment.get("size")),
            "Awaiting AI Extraction",
            datetime.now(timezone.utc).isoformat(),
            sharepoint_item_id,
            sharepoint_web_url,
        )
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO invoices (
                        message_id, attachment_id, internet_message_id,
                        sender_name, sender_address, subject, received_at,
                        original_filename, stored_path, size_bytes, status, created_at,
                        sharepoint_item_id, sharepoint_web_url
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (message_id, attachment_id) DO UPDATE
                    SET stored_path = CASE
                            WHEN invoices.sharepoint_item_id IS NULL
                            THEN EXCLUDED.stored_path
                            ELSE invoices.stored_path
                        END,
                        sharepoint_item_id = COALESCE(
                            invoices.sharepoint_item_id,
                            EXCLUDED.sharepoint_item_id
                        ),
                        sharepoint_web_url = COALESCE(
                            invoices.sharepoint_web_url,
                            EXCLUDED.sharepoint_web_url
                        )
                    RETURNING {_SELECT_COLUMNS}
                    """,
                    values,
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Invoice record could not be persisted.")
        return self._record(row)

    def list(self, limit: int = 100) -> list[InvoiceRecord]:
        self._validate_limit(limit, maximum=500)
        return self._query_many(
            f"SELECT {_SELECT_COLUMNS} FROM invoices "
            "ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )

    def list_by_status(self, statuses: list[str], limit: int = 200) -> list[InvoiceRecord]:
        if not statuses:
            return []
        self._validate_limit(limit, maximum=500)
        return self._query_many(
            f"SELECT {_SELECT_COLUMNS} FROM invoices "
            "WHERE status = ANY(%s) ORDER BY created_at DESC LIMIT %s",
            (statuses, limit),
        )

    def get(self, invoice_id: int) -> InvoiceRecord | None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM invoices WHERE id = %s",
                    (invoice_id,),
                )
                row = cursor.fetchone()
        return self._record(row) if row is not None else None

    def get_by_sharepoint_item_id(self, item_id: str) -> InvoiceRecord | None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM invoices "
                    "WHERE sharepoint_item_id = %s LIMIT 1",
                    (item_id,),
                )
                row = cursor.fetchone()
        return self._record(row) if row is not None else None

    def get_by_source_attachment(
        self, message_id: str, attachment_id: str
    ) -> InvoiceRecord | None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM invoices "
                    "WHERE message_id = %s AND attachment_id = %s LIMIT 1",
                    (message_id, attachment_id),
                )
                row = cursor.fetchone()
        return self._record(row) if row is not None else None

    def update_status(self, invoice_id: int, status: str) -> None:
        self.update_fields(invoice_id, status=status)

    def update_sharepoint(
        self,
        invoice_id: int,
        *,
        sharepoint_item_id: str,
        sharepoint_web_url: str | None,
        status: str,
    ) -> None:
        self.update_fields(
            invoice_id,
            sharepoint_item_id=sharepoint_item_id,
            sharepoint_web_url=sharepoint_web_url,
            status=status,
        )

    def update_fields(self, invoice_id: int, **fields: object) -> InvoiceRecord:
        unknown = set(fields) - _UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(f"Unknown invoice fields: {', '.join(sorted(unknown))}.")
        if not fields:
            record = self.get(invoice_id)
            if record is None:
                raise KeyError(f"Invoice {invoice_id} was not found.")
            return record

        # Identifiers come only from the static dataclass whitelist; values remain bound.
        assignments = ", ".join(f"{name} = %s" for name in fields)
        parameters = (*fields.values(), invoice_id)
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE invoices SET {assignments} WHERE id = %s "
                    f"RETURNING {_SELECT_COLUMNS}",
                    parameters,
                )
                row = cursor.fetchone()
        if row is None:
            raise KeyError(f"Invoice {invoice_id} was not found.")
        return self._record(row)

    def _query_many(
        self, sql: str, parameters: tuple[object, ...]
    ) -> list[InvoiceRecord]:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(sql, parameters)
                rows = cursor.fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _record(row: Mapping[str, object]) -> InvoiceRecord:
        return InvoiceRecord(**{column: row[column] for column in _RECORD_COLUMNS})

    @staticmethod
    def _validate_limit(limit: int, *, maximum: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
            raise ValueError(f"Invoice limit must be between 1 and {maximum}.")

    @staticmethod
    def _required_string(data: dict[str, object], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Outlook data is missing required field '{key}'.")
        return value

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @classmethod
    def _sender(cls, message: dict[str, object]) -> tuple[str | None, str | None]:
        sender = message.get("from")
        if not isinstance(sender, dict):
            return None, None
        email = sender.get("emailAddress")
        if not isinstance(email, dict):
            return None, None
        return cls._optional_string(email.get("name")), cls._optional_string(
            email.get("address")
        )


def create_postgres_invoice_store(
    environ: Mapping[str, str] | None = None,
    *,
    initialize_schema: bool = True,
) -> PostgresInvoiceStore:
    environment = os.environ if environ is None else environ
    auto_migrate = environment.get(
        "AZURE_POSTGRES_AUTO_MIGRATE", "false"
    ).strip().lower() in {"1", "true", "yes"}
    return PostgresInvoiceStore(
        PostgresSettings.from_env(environment),
        initialize_schema=initialize_schema and auto_migrate,
    )
