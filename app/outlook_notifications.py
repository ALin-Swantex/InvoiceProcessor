from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS outlook_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    change_type TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    payload TEXT NOT NULL,
    UNIQUE(subscription_id, resource, change_type)
);

CREATE INDEX IF NOT EXISTS idx_outlook_notifications_status
ON outlook_notifications(status, received_at);
"""


@dataclass(frozen=True)
class QueuedNotification:
    id: int
    subscription_id: str
    message_id: str
    resource: str
    change_type: str
    received_at: str
    status: str
    attempts: int = 0
    last_error: str | None = None


class OutlookNotificationStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(outlook_notifications)"
                ).fetchall()
            }
            if "attempts" not in columns:
                connection.execute(
                    "ALTER TABLE outlook_notifications "
                    "ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "last_error" not in columns:
                connection.execute(
                    "ALTER TABLE outlook_notifications ADD COLUMN last_error TEXT"
                )
            connection.commit()

    def enqueue(
        self,
        *,
        subscription_id: str,
        message_id: str,
        resource: str,
        change_type: str,
        payload: dict[str, Any],
    ) -> bool:
        received_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO outlook_notifications (
                    subscription_id, message_id, resource, change_type,
                    received_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    subscription_id,
                    message_id,
                    resource,
                    change_type,
                    received_at,
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def list(self, *, status: str = "pending", limit: int = 100) -> list[QueuedNotification]:
        if limit < 1 or limit > 500:
            raise ValueError("Notification limit must be between 1 and 500.")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, subscription_id, message_id, resource, change_type,
                       received_at, status, attempts, last_error
                FROM outlook_notifications
                WHERE status = ?
                ORDER BY received_at
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        return [QueuedNotification(**dict(row)) for row in rows]

    def claim_next(self) -> QueuedNotification | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, subscription_id, message_id, resource, change_type,
                       received_at, status, attempts, last_error
                FROM outlook_notifications
                WHERE status = 'pending'
                ORDER BY received_at
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            updated = connection.execute(
                """
                UPDATE outlook_notifications
                SET status = 'processing', attempts = attempts + 1, last_error = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (row["id"],),
            )
            connection.commit()
            if updated.rowcount != 1:
                return None
            claimed = dict(row)
            claimed["status"] = "processing"
            claimed["attempts"] += 1
            return QueuedNotification(**claimed)

    def mark_completed(self, notification_id: int) -> None:
        self._set_status(notification_id, "completed")

    def mark_failed(self, notification_id: int, error: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE outlook_notifications
                SET status = 'failed', last_error = ?
                WHERE id = ?
                """,
                (error[:2000], notification_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise KeyError(f"Notification {notification_id} was not found.")

    def retry_failed(self, notification_id: int) -> None:
        self._set_status(notification_id, "pending")

    def _set_status(self, notification_id: int, status: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE outlook_notifications SET status = ? WHERE id = ?",
                (status, notification_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise KeyError(f"Notification {notification_id} was not found.")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection


def extract_message_id(notification: dict[str, Any]) -> str | None:
    resource_data = notification.get("resourceData")
    if isinstance(resource_data, dict):
        message_id = resource_data.get("id")
        if isinstance(message_id, str) and message_id:
            return message_id

    resource = notification.get("resource")
    if not isinstance(resource, str) or not resource:
        return None
    return resource.rstrip("/").rsplit("/", 1)[-1] or None
