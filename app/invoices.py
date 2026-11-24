from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    internet_message_id TEXT,
    sender_name TEXT,
    sender_address TEXT,
    subject TEXT,
    received_at TEXT,
    original_filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    size_bytes INTEGER,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(message_id, attachment_id)
);

CREATE INDEX IF NOT EXISTS idx_invoices_created_at
ON invoices(created_at DESC);
"""

# Columns added after the initial release. Stored as name -> SQL type so
# they can be added with ALTER TABLE without a separate migration tool.
# Every store instance ensures these exist on start-up.
LIFECYCLE_COLUMNS: dict[str, str] = {
    "document_type": "TEXT NOT NULL DEFAULT 'invoice'",
    "document_classification_confidence": "REAL",
    "document_classification_reason": "TEXT",
    "sharepoint_item_id": "TEXT",
    "sharepoint_web_url": "TEXT",
    "irj_number": "TEXT",
    "company": "TEXT",
    "supplier": "TEXT",
    "supplier_invoice_number": "TEXT",
    "po_number": "TEXT",
    "invoice_type": "TEXT",
    "invoice_date": "TEXT",
    "invoice_value": "REAL",
    "currency": "TEXT",
    "ai_confidence": "REAL",
    "ai_field_confidences": "TEXT",
    "ai_review_warnings": "TEXT",
    "approver1_name": "TEXT",
    "approver1_email": "TEXT",
    "approver1_decision": "TEXT",
    "approver1_date": "TEXT",
    "approver1_comments": "TEXT",
    "approver2_name": "TEXT",
    "approver2_email": "TEXT",
    "approver2_decision": "TEXT",
    "approver2_date": "TEXT",
    "approver2_comments": "TEXT",
    "po_query_notes": "TEXT",
    "po_query_category": "TEXT",
    "po_query_contact": "TEXT",
    "payment_date": "TEXT",
    "supplier_account_number": "TEXT",
    "payment_reference": "TEXT",
    "payment_method": "TEXT",
    "paid_by": "TEXT",
    "reconciliation_date": "TEXT",
    "reconciliation_notes": "TEXT",
    "reconciled_by": "TEXT",
    "reconciled_at": "TEXT",
    "rejection_reason": "TEXT",
    "review_reason": "TEXT",
    "review_return_status": "TEXT",
    "duplicate_of_invoice_id": "INTEGER",
    "hold_reason": "TEXT",
    "hold_level": "INTEGER",
    "approval_requested_at": "TEXT",
    "approval_reminder_sent_at": "TEXT",
    "approval_hold_started_at": "TEXT",
    "approval_hold_reminder_sent_at": "TEXT",
    "sage_registered_at": "TEXT",
    "sage_reference": "TEXT",
    "sage_registered_by": "TEXT",
    "is_foreign_payment": "INTEGER",
    "payment_route_decided_at": "TEXT",
    "payment_route_decided_by": "TEXT",
    "foreign_allocation_date": "TEXT",
    "foreign_allocation_reference": "TEXT",
    "foreign_allocated_by": "TEXT",
    "cancelled_at": "TEXT",
    "cancelled_by": "TEXT",
    "cancellation_reason": "TEXT",
    "extraction_model": "TEXT",
    "extraction_prompt_version": "TEXT",
    "extracted_fields_json": "TEXT",
    "routing_explanation": "TEXT",
    "reviewed_at": "TEXT",
    "reviewed_by": "TEXT",
    "correction_reason": "TEXT",
    "corrected_fields_json": "TEXT",
}


@dataclass(frozen=True)
class InvoiceRecord:
    id: int
    message_id: str
    attachment_id: str
    internet_message_id: str | None
    sender_name: str | None
    sender_address: str | None
    subject: str | None
    received_at: str | None
    original_filename: str
    stored_path: str
    size_bytes: int | None
    status: str
    created_at: str
    document_type: str = "invoice"
    document_classification_confidence: float | None = None
    document_classification_reason: str | None = None
    sharepoint_item_id: str | None = None
    sharepoint_web_url: str | None = None
    irj_number: str | None = None
    company: str | None = None
    supplier: str | None = None
    supplier_invoice_number: str | None = None
    po_number: str | None = None
    invoice_type: str | None = None
    invoice_date: str | None = None
    invoice_value: float | None = None
    currency: str | None = None
    ai_confidence: float | None = None
    ai_field_confidences: str | None = None
    ai_review_warnings: str | None = None
    approver1_name: str | None = None
    approver1_email: str | None = None
    approver1_decision: str | None = None
    approver1_date: str | None = None
    approver1_comments: str | None = None
    approver2_name: str | None = None
    approver2_email: str | None = None
    approver2_decision: str | None = None
    approver2_date: str | None = None
    approver2_comments: str | None = None
    po_query_notes: str | None = None
    po_query_category: str | None = None
    po_query_contact: str | None = None
    payment_date: str | None = None
    supplier_account_number: str | None = None
    payment_reference: str | None = None
    payment_method: str | None = None
    paid_by: str | None = None
    reconciliation_date: str | None = None
    reconciliation_notes: str | None = None
    reconciled_by: str | None = None
    reconciled_at: str | None = None
    rejection_reason: str | None = None
    review_reason: str | None = None
    review_return_status: str | None = None
    duplicate_of_invoice_id: int | None = None
    hold_reason: str | None = None
    hold_level: int | None = None
    approval_requested_at: str | None = None
    approval_reminder_sent_at: str | None = None
    approval_hold_started_at: str | None = None
    approval_hold_reminder_sent_at: str | None = None
    sage_registered_at: str | None = None
    sage_reference: str | None = None
    sage_registered_by: str | None = None
    is_foreign_payment: int | None = None
    payment_route_decided_at: str | None = None
    payment_route_decided_by: str | None = None
    foreign_allocation_date: str | None = None
    foreign_allocation_reference: str | None = None
    foreign_allocated_by: str | None = None
    cancelled_at: str | None = None
    cancelled_by: str | None = None
    cancellation_reason: str | None = None
    extraction_model: str | None = None
    extraction_prompt_version: str | None = None
    extracted_fields_json: str | None = None
    routing_explanation: str | None = None
    reviewed_at: str | None = None
    reviewed_by: str | None = None
    correction_reason: str | None = None
    corrected_fields_json: str | None = None


# Statuses where an invoice needs no further action. The work queues show every
# invoice NOT in this set, plus only the most recent finished ones, so an old
# invoice that is still waiting on someone never drops out of view.
TERMINAL_STATUSES = (
    "Reconciled / Complete",
    "Statement Filed",
    "Rejected",
    "Cancelled - Duplicate",
)


@dataclass(frozen=True)
class MetricInvoice:
    """The few columns the Admin metrics need (not the full 80-column record)."""

    id: int
    irj_number: str | None
    company: str | None
    supplier: str | None
    invoice_date: str | None
    invoice_value: float | None
    currency: str | None
    status: str
    document_type: str
    created_at: str
    supplier_account_number: str | None


METRIC_COLUMNS = tuple(MetricInvoice.__dataclass_fields__)


def _norm(value: object) -> str:
    """Trimmed value for a lower(...) = lower(?) comparison. Python's casefold()
    is not used: it turns "ß" into "ss", which SQL lower() does not, so a
    German supplier name would never match itself."""
    return str(value or "").strip()


class InvoiceStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_lifecycle_columns(connection)
            connection.execute(
                """
                UPDATE invoices
                SET irj_number = substr(irj_number, 5)
                WHERE irj_number GLOB 'IRJ-[0-9][0-9][0-9][0-9][0-9][0-9]'
                  AND length(irj_number) = 10
                """
            )
            connection.commit()

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
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        sharepoint_item_id = self._optional_string(
            attachment.get("sharepoint_item_id")
        )
        sharepoint_web_url = self._optional_string(
            attachment.get("sharepoint_web_url")
        )

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO invoices (
                    message_id, attachment_id, internet_message_id,
                    sender_name, sender_address, subject, received_at,
                    original_filename, stored_path, size_bytes, status, created_at,
                    sharepoint_item_id, sharepoint_web_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id, attachment_id) DO UPDATE SET
                    stored_path = CASE
                        WHEN invoices.sharepoint_item_id IS NULL
                        THEN excluded.stored_path
                        ELSE invoices.stored_path
                    END,
                    sharepoint_item_id = COALESCE(
                        invoices.sharepoint_item_id,
                        excluded.sharepoint_item_id
                    ),
                    sharepoint_web_url = COALESCE(
                        invoices.sharepoint_web_url,
                        excluded.sharepoint_web_url
                    )
                """,
                (
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
                    created_at,
                    sharepoint_item_id,
                    sharepoint_web_url,
                ),
            )
            connection.commit()
            row = connection.execute(
                """
                SELECT * FROM invoices
                WHERE message_id = ? AND attachment_id = ?
                """,
                (message_id, attachment_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("Invoice record could not be persisted.")
        return InvoiceRecord(**dict(row))

    def list(self, limit: int = 100) -> list[InvoiceRecord]:
        if limit < 1 or limit > 500:
            raise ValueError("Invoice limit must be between 1 and 500.")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM invoices ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [InvoiceRecord(**dict(row)) for row in rows]

    def list_all(self) -> list[InvoiceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM invoices ORDER BY created_at DESC"
            ).fetchall()
        return [InvoiceRecord(**dict(row)) for row in rows]

    def list_all_with_status(self, statuses: list[str]) -> list[InvoiceRecord]:
        """Every invoice in the given statuses (no row limit)."""
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM invoices WHERE status IN ({placeholders}) "
                "ORDER BY created_at DESC",
                tuple(statuses),
            ).fetchall()
        return [InvoiceRecord(**dict(row)) for row in rows]

    def highest_irj_number(self, company: str) -> int | None:
        """The highest all-digit IRJ number assigned to the company, if any."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT irj_number FROM invoices "
                "WHERE lower(company) = lower(?) AND irj_number IS NOT NULL",
                (company,),
            ).fetchall()
        numbers = [int(r[0]) for r in rows if str(r[0]).isdigit()]
        return max(numbers) if numbers else None

    def list_workqueue(
        self,
        limit: int = 500,
        approver: tuple[int, str] | None = None,
    ) -> list[InvoiceRecord]:
        """All unfinished invoices plus the newest `limit` finished ones."""
        if limit < 1 or limit > 500:
            raise ValueError("Invoice limit must be between 1 and 500.")
        terminal = ",".join("?" for _ in TERMINAL_STATUSES)
        scope, scope_args = "", ()
        if approver is not None:
            level, email = approver
            if level not in (1, 2):
                raise ValueError("Approval level must be 1 or 2.")
            scope = f"lower(trim(approver{level}_email)) = lower(trim(?)) AND "
            scope_args = (email,)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM invoices WHERE {scope}(status NOT IN ({terminal}) "
                f"OR id IN (SELECT id FROM invoices WHERE {scope}status IN ({terminal}) "
                "ORDER BY created_at DESC LIMIT ?)) ORDER BY created_at DESC",
                (*scope_args, *TERMINAL_STATUSES, *scope_args, *TERMINAL_STATUSES, limit),
            ).fetchall()
        return [InvoiceRecord(**dict(row)) for row in rows]

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
            clauses.append("irj_number = ?")
            args.append(irj_number)
        if company:
            clauses.append("lower(trim(company)) = lower(?)")
            args.append(_norm(company))
        if supplier:
            clauses.append("lower(trim(supplier)) = lower(?)")
            args.append(_norm(supplier))
        if approver is not None:
            level, email = approver
            if level not in (1, 2):
                raise ValueError("Approval level must be 1 or 2.")
            clauses.append(f"lower(trim(approver{level}_email)) = lower(trim(?))")
            args.append(email)
        if not clauses:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM invoices WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC",
                tuple(args),
            ).fetchall()
        return [InvoiceRecord(**dict(row)) for row in rows]

    def find_duplicate_by_number(
        self, *, exclude_id: int, company: str, supplier: str, number: str
    ) -> InvoiceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM invoices WHERE id <> ? "
                "AND lower(trim(coalesce(company, ''))) = lower(?) "
                "AND lower(trim(coalesce(supplier, ''))) = lower(?) "
                "AND lower(trim(coalesce(supplier_invoice_number, ''))) = lower(?) "
                "ORDER BY created_at DESC LIMIT 1",
                (exclude_id, _norm(company), _norm(supplier), _norm(number)),
            ).fetchone()
        return InvoiceRecord(**dict(row)) if row is not None else None

    def find_duplicate_by_source(
        self, invoice: InvoiceRecord, company: str
    ) -> InvoiceRecord | None:
        """Newest other invoice in the company from the same email attachment,
        or with the same filename and size."""
        filename = _norm(invoice.original_filename)
        conditions: list[str] = []
        args: list[object] = []
        if invoice.internet_message_id:
            conditions.append("(internet_message_id = ? AND attachment_id = ?)")
            args += [invoice.internet_message_id, invoice.attachment_id]
        if filename and invoice.size_bytes is not None:
            conditions.append(
                "(lower(trim(coalesce(original_filename, ''))) = lower(?) AND size_bytes = ?)"
            )
            args += [filename, invoice.size_bytes]
        if not conditions:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM invoices WHERE id <> ? "
                "AND lower(trim(coalesce(company, ''))) = lower(?) "
                f"AND ({' OR '.join(conditions)}) "
                "ORDER BY created_at DESC LIMIT 1",
                (invoice.id, _norm(company), *args),
            ).fetchone()
        return InvoiceRecord(**dict(row)) if row is not None else None

    def list_by_size(self, size_bytes: int, exclude_id: int) -> list[InvoiceRecord]:
        """Invoices of this size, plus any whose size was never recorded."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM invoices WHERE (size_bytes = ? OR size_bytes IS NULL) "
                "AND id <> ? "
                "ORDER BY created_at DESC",
                (size_bytes, exclude_id),
            ).fetchall()
        return [InvoiceRecord(**dict(row)) for row in rows]

    def list_for_metrics(self) -> list[MetricInvoice]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(METRIC_COLUMNS)} FROM invoices "
                "WHERE document_type = 'invoice'"
            ).fetchall()
        return [MetricInvoice(**dict(row)) for row in rows]

    def get(self, invoice_id: int) -> InvoiceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
        return InvoiceRecord(**dict(row)) if row is not None else None

    def get_by_irj_number(
        self, irj_number: str, company: str | None = None
    ) -> InvoiceRecord | None:
        with self._connect() as connection:
            if company is None:
                row = connection.execute(
                    """
                    SELECT * FROM invoices
                    WHERE upper(trim(irj_number)) = upper(trim(?))
                    ORDER BY id LIMIT 1
                    """,
                    (irj_number,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM invoices
                    WHERE upper(trim(irj_number)) = upper(trim(?))
                      AND company = ? COLLATE NOCASE
                    ORDER BY id LIMIT 1
                    """,
                    (irj_number, company),
                ).fetchone()
        return InvoiceRecord(**dict(row)) if row is not None else None

    def get_by_sharepoint_item_id(self, item_id: str) -> InvoiceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM invoices WHERE sharepoint_item_id = ? LIMIT 1",
                (item_id,),
            ).fetchone()
        return InvoiceRecord(**dict(row)) if row is not None else None

    def get_by_source_attachment(
        self, message_id: str, attachment_id: str
    ) -> InvoiceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM invoices
                WHERE message_id = ? AND attachment_id = ?
                LIMIT 1
                """,
                (message_id, attachment_id),
            ).fetchone()
        return InvoiceRecord(**dict(row)) if row is not None else None

    def update_fields(self, invoice_id: int, **fields: object) -> InvoiceRecord:
        """Generic column update used by the invoice lifecycle module for
        every state transition (routing, approvals, payment, reconciliation).

        Only columns declared on InvoiceRecord may be updated; anything else
        raises immediately rather than silently failing.
        """
        allowed = set(InvoiceRecord.__dataclass_fields__) - {"id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown invoice fields: {', '.join(sorted(unknown))}.")
        if not fields:
            record = self.get(invoice_id)
            if record is None:
                raise KeyError(f"Invoice {invoice_id} was not found.")
            return record

        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values())
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE invoices SET {assignments} WHERE id = ?",
                (*values, invoice_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise KeyError(f"Invoice {invoice_id} was not found.")
            row = connection.execute(
                "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
        return InvoiceRecord(**dict(row))

    def update_fields_if_status(
        self,
        invoice_id: int,
        expected_status: str,
        **fields: object,
    ) -> InvoiceRecord | None:
        """Atomically update an invoice only while it remains in the expected state."""
        allowed = set(InvoiceRecord.__dataclass_fields__) - {"id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown invoice fields: {', '.join(sorted(unknown))}.")
        if not fields:
            record = self.get(invoice_id)
            return record if record is not None and record.status == expected_status else None

        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values())
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE invoices
                SET {assignments}
                WHERE id = ? AND status = ?
                """,
                (*values, invoice_id, expected_status),
            )
            connection.commit()
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
        return InvoiceRecord(**dict(row))

    def delete(self, invoice_id: int) -> None:
        with self._connect() as connection:
            # Match PostgreSQL, whose foreign key would refuse the delete
            # while another invoice is marked as a duplicate of this one.
            connection.execute(
                "UPDATE invoices SET duplicate_of_invoice_id = NULL "
                "WHERE duplicate_of_invoice_id = ?",
                (invoice_id,),
            )
            cursor = connection.execute(
                "DELETE FROM invoices WHERE id = ?",
                (invoice_id,),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise KeyError(f"Invoice {invoice_id} was not found.")

    @staticmethod
    def _ensure_lifecycle_columns(connection: sqlite3.Connection) -> None:
        existing = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(invoices)").fetchall()
        }
        for column, sql_type in LIFECYCLE_COLUMNS.items():
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE invoices ADD COLUMN {column} {sql_type}"
                )

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
        return value if isinstance(value, int) else None

    @classmethod
    def _sender(
        cls, message: dict[str, object]
    ) -> tuple[str | None, str | None]:
        sender = message.get("from")
        if not isinstance(sender, dict):
            return None, None
        email_address = sender.get("emailAddress")
        if not isinstance(email_address, dict):
            return None, None
        return (
            cls._optional_string(email_address.get("name")),
            cls._optional_string(email_address.get("address")),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection
