from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from app.invoices import (
    METRIC_COLUMNS,
    TERMINAL_STATUSES,
    InvoiceRecord,
    InvoiceStore,
    MetricInvoice,
    _norm,
)
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
        created_at: str | None = None,
    ) -> InvoiceRecord:
        """Insert the invoice, or return the existing row for the same message
        and attachment. A caller can pass ``created_at`` and compare it with
        the returned record's to tell whether this call inserted the row."""
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
            created_at or datetime.now(timezone.utc).isoformat(),
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

    def list_all(self) -> list[InvoiceRecord]:
        return self._query_many(
            f"SELECT {_SELECT_COLUMNS} FROM invoices ORDER BY created_at DESC",
            (),
        )

    def list_all_with_status(self, statuses: list[str]) -> list[InvoiceRecord]:
        """Every invoice in the given statuses (no row limit)."""
        if not statuses:
            return []
        return self._query_many(
            f"SELECT {_SELECT_COLUMNS} FROM invoices "
            "WHERE status = ANY(%s) ORDER BY created_at DESC",
            (list(statuses),),
        )

    def highest_irj_number(self, company: str) -> int | None:
        """The highest all-digit IRJ number assigned to the company, if any."""
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT max(CASE WHEN irj_number ~ '^[0-9]+$' "
                    "THEN irj_number::bigint END) AS highest FROM invoices "
                    "WHERE lower(company) = lower(%s)",
                    (company,),
                )
                row = cursor.fetchone()
        return int(row["highest"]) if row and row["highest"] is not None else None

    def list_workqueue(
        self,
        limit: int = 500,
        approver: tuple[int, str] | None = None,
    ) -> list[InvoiceRecord]:
        """All unfinished invoices plus the newest `limit` finished ones."""
        self._validate_limit(limit, maximum=500)
        scope, scope_args = "", ()
        if approver is not None:
            level, email = approver
            if level not in (1, 2):
                raise ValueError("Approval level must be 1 or 2.")
            scope = f"lower(btrim(approver{level}_email)) = lower(btrim(%s)) AND "
            scope_args = (email,)
        terminal = list(TERMINAL_STATUSES)
        return self._query_many(
            f"SELECT {_SELECT_COLUMNS} FROM invoices WHERE {scope}"
            "(status <> ALL(%s) OR id IN (SELECT id FROM invoices "
            f"WHERE {scope}status = ANY(%s) ORDER BY created_at DESC LIMIT %s)) "
            "ORDER BY created_at DESC",
            (*scope_args, terminal, *scope_args, terminal, limit),
        )

    def search(
        self,
        *,
        irj_number: str | None = None,
        company: str | None = None,
        supplier: str | None = None,
        approver: tuple[int, str] | None = None,
    ) -> list[InvoiceRecord]:
        """Exact (trimmed, case-insensitive) match on the filters given."""
        clauses: list[str] = []
        args: list[object] = []
        if irj_number:
            clauses.append("irj_number = %s")
            args.append(irj_number)
        if company:
            clauses.append("lower(btrim(company)) = lower(%s)")
            args.append(_norm(company))
        if supplier:
            clauses.append("lower(btrim(supplier)) = lower(%s)")
            args.append(_norm(supplier))
        if approver is not None:
            level, email = approver
            if level not in (1, 2):
                raise ValueError("Approval level must be 1 or 2.")
            clauses.append(f"lower(btrim(approver{level}_email)) = lower(btrim(%s))")
            args.append(email)
        if not clauses:
            return []
        return self._query_many(
            f"SELECT {_SELECT_COLUMNS} FROM invoices WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC",
            tuple(args),
        )

    def find_duplicate_by_number(
        self, *, exclude_id: int, company: str, supplier: str, number: str
    ) -> InvoiceRecord | None:
        rows = self._query_many(
            f"SELECT {_SELECT_COLUMNS} FROM invoices WHERE id <> %s "
            "AND lower(btrim(coalesce(company, ''))) = lower(%s) "
            "AND lower(btrim(coalesce(supplier, ''))) = lower(%s) "
            "AND lower(btrim(coalesce(supplier_invoice_number, ''))) = lower(%s) "
            "ORDER BY created_at DESC LIMIT 1",
            (exclude_id, _norm(company), _norm(supplier), _norm(number)),
        )
        return rows[0] if rows else None

    def find_duplicate_by_source(
        self, invoice: InvoiceRecord, company: str
    ) -> InvoiceRecord | None:
        """Newest other invoice in the company from the same email attachment,
        or with the same filename and size."""
        filename = _norm(invoice.original_filename)
        conditions: list[str] = []
        args: list[object] = []
        if invoice.internet_message_id:
            conditions.append("(internet_message_id = %s AND attachment_id = %s)")
            args += [invoice.internet_message_id, invoice.attachment_id]
        if filename and invoice.size_bytes is not None:
            conditions.append(
                "(lower(btrim(coalesce(original_filename, ''))) = lower(%s) "
                "AND size_bytes = %s)"
            )
            args += [filename, invoice.size_bytes]
        if not conditions:
            return None
        rows = self._query_many(
            f"SELECT {_SELECT_COLUMNS} FROM invoices WHERE id <> %s "
            "AND lower(btrim(coalesce(company, ''))) = lower(%s) "
            f"AND ({' OR '.join(conditions)}) "
            "ORDER BY created_at DESC LIMIT 1",
            (invoice.id, _norm(company), *args),
        )
        return rows[0] if rows else None

    def list_by_size(self, size_bytes: int, exclude_id: int) -> list[InvoiceRecord]:
        """Invoices of this size, plus any whose size was never recorded."""
        return self._query_many(
            f"SELECT {_SELECT_COLUMNS} FROM invoices "
            "WHERE (size_bytes = %s OR size_bytes IS NULL) AND id <> %s "
            "ORDER BY created_at DESC",
            (size_bytes, exclude_id),
        )

    def list_for_metrics(self) -> list[MetricInvoice]:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {', '.join(METRIC_COLUMNS)} FROM invoices "
                    "WHERE document_type = 'invoice'"
                )
                rows = cursor.fetchall()
        return [MetricInvoice(**{c: row[c] for c in METRIC_COLUMNS}) for row in rows]

    def get(self, invoice_id: int) -> InvoiceRecord | None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM invoices WHERE id = %s",
                    (invoice_id,),
                )
                row = cursor.fetchone()
        return self._record(row) if row is not None else None

    def get_by_irj_number(
        self, irj_number: str, company: str | None = None
    ) -> InvoiceRecord | None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                if company is None:
                    cursor.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM invoices "
                        "WHERE upper(btrim(irj_number)) = upper(btrim(%s)) "
                        "ORDER BY id LIMIT 1",
                        (irj_number,),
                    )
                else:
                    cursor.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM invoices "
                        "WHERE upper(btrim(irj_number)) = upper(btrim(%s)) "
                        "AND lower(company) = lower(%s) ORDER BY id LIMIT 1",
                        (irj_number, company),
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

    def update_fields_if_status(
        self,
        invoice_id: int,
        expected_status: str,
        **fields: object,
    ) -> InvoiceRecord | None:
        """Atomically update an invoice only while it remains in the expected state."""
        unknown = set(fields) - _UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(f"Unknown invoice fields: {', '.join(sorted(unknown))}.")
        if not fields:
            record = self.get(invoice_id)
            return record if record is not None and record.status == expected_status else None

        assignments = ", ".join(f"{name} = %s" for name in fields)
        parameters = (*fields.values(), invoice_id, expected_status)
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE invoices SET {assignments} "
                    "WHERE id = %s AND status = %s "
                    f"RETURNING {_SELECT_COLUMNS}",
                    parameters,
                )
                row = cursor.fetchone()
        return self._record(row) if row is not None else None

    def delete(self, invoice_id: int) -> None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                # The duplicate_of_invoice_id foreign key has no ON DELETE
                # action, so clear references to this invoice first (same
                # transaction).
                cursor.execute(
                    "UPDATE invoices SET duplicate_of_invoice_id = NULL "
                    "WHERE duplicate_of_invoice_id = %s",
                    (invoice_id,),
                )
                cursor.execute(
                    "DELETE FROM invoices WHERE id = %s RETURNING id",
                    (invoice_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise KeyError(f"Invoice {invoice_id} was not found.")

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

    _required_string = staticmethod(InvoiceStore._required_string)
    _optional_string = staticmethod(InvoiceStore._optional_string)

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    _sender = InvoiceStore._sender


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
