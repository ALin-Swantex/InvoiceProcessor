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
    "rejection_reason": "TEXT",
    "review_reason": "TEXT",
    "review_return_status": "TEXT",
    "duplicate_of_invoice_id": "INTEGER",
    "hold_reason": "TEXT",
    "hold_level": "INTEGER",
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
    rejection_reason: str | None = None
    review_reason: str | None = None
    review_return_status: str | None = None
    duplicate_of_invoice_id: int | None = None
    hold_reason: str | None = None
    hold_level: int | None = None
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


class InvoiceStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_lifecycle_columns(connection)
            connection.commit()

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
        created_at = datetime.now(timezone.utc).isoformat()

        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO invoices (
                    message_id, attachment_id, internet_message_id,
                    sender_name, sender_address, subject, received_at,
                    original_filename, stored_path, size_bytes, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def list_by_status(self, statuses: list[str], limit: int = 200) -> list[InvoiceRecord]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM invoices
                WHERE status IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (*statuses, limit),
            ).fetchall()
        return [InvoiceRecord(**dict(row)) for row in rows]

    def get(self, invoice_id: int) -> InvoiceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
        return InvoiceRecord(**dict(row)) if row is not None else None

    def get_by_sharepoint_item_id(self, item_id: str) -> InvoiceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM invoices WHERE sharepoint_item_id = ? LIMIT 1",
                (item_id,),
            ).fetchone()
        return InvoiceRecord(**dict(row)) if row is not None else None

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
        """Record the SharePoint item ID and web URL after a successful upload
        or move, and update the processing status."""
        self.update_fields(
            invoice_id,
            sharepoint_item_id=sharepoint_item_id,
            sharepoint_web_url=sharepoint_web_url,
            status=status,
        )

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
