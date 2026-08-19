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


class InvoiceStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

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

    def get(self, invoice_id: int) -> InvoiceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
        return InvoiceRecord(**dict(row)) if row is not None else None

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

