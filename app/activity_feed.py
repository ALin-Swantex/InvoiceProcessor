from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    target_role TEXT NOT NULL,
    message TEXT NOT NULL,
    invoice_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_events_id
ON activity_events(id);

CREATE INDEX IF NOT EXISTS idx_activity_events_invoice
ON activity_events(invoice_id, id);

CREATE TABLE IF NOT EXISTS invoice_email_stages (
    invoice_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (invoice_id, stage)
);
"""

# Valid target_role values used throughout the app. "all" is broadcast to
# every connected frontend session regardless of the selected role.
ROLE_PURCHASE_LEDGER = "purchase_ledger"
ROLE_APPROVER_1 = "approver1"
ROLE_APPROVER_2 = "approver2"
ROLE_PURCHASING = "purchasing"
ROLE_ALL = "all"


@dataclass(frozen=True)
class ActivityEvent:
    id: int
    event_type: str
    target_role: str
    message: str
    invoice_id: int | None
    created_at: str


class ActivityFeedStore:
    """A lightweight, append-only event log used to drive the frontend's
    toast notification system (e.g. "Approver 1 has a new invoice awaiting
    approval"). This is a local development stand-in for a real
    notification channel (e.g. Microsoft Graph sendMail or Teams webhook)."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def add_event(
        self,
        *,
        event_type: str,
        target_role: str,
        message: str,
        invoice_id: int | None = None,
    ) -> ActivityEvent:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO activity_events (
                    event_type, target_role, message, invoice_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_type, target_role, message, invoice_id, created_at),
            )
            connection.commit()
            event_id = cursor.lastrowid
        return ActivityEvent(
            id=event_id,
            event_type=event_type,
            target_role=target_role,
            message=message,
            invoice_id=invoice_id,
            created_at=created_at,
        )

    def list_since(self, since_id: int = 0, limit: int = 50) -> list[ActivityEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event_type, target_role, message, invoice_id, created_at
                FROM activity_events
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (since_id, limit),
            ).fetchall()
        return [ActivityEvent(**dict(row)) for row in rows]

    def list_for_invoice(
        self, invoice_id: int, limit: int = 200
    ) -> list[ActivityEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event_type, target_role, message, invoice_id, created_at
                FROM (
                    SELECT id, event_type, target_role, message,
                           invoice_id, created_at
                    FROM activity_events
                    WHERE invoice_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (invoice_id, limit),
            ).fetchall()
        return [ActivityEvent(**dict(row)) for row in rows]

    def claim_email_stage(self, invoice_id: int, stage: str) -> bool:
        """Atomically reserve one email delivery attempt for an invoice stage."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO invoice_email_stages (
                    invoice_id, stage, claimed_at
                ) VALUES (?, ?, ?)
                """,
                (invoice_id, stage, datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()
        return cursor.rowcount == 1

    def release_email_stage(self, invoice_id: int, stage: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM invoice_email_stages
                WHERE invoice_id = ? AND stage = ?
                """,
                (invoice_id, stage),
            )
            connection.commit()

    def latest_id(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS latest FROM activity_events"
            ).fetchone()
        return int(row["latest"])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection
