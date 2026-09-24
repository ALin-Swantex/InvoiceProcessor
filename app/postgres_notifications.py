from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.outlook_notifications import QueuedNotification
from app.postgres_settings import ConnectionFactory, PostgresSettings, postgres_connection_factory


class PostgresOutlookNotificationStore:
    """PostgreSQL-backed Outlook queue with atomic worker claiming."""

    def __init__(
        self,
        settings: PostgresSettings | None = None,
        *,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._connection_factory = connection_factory or postgres_connection_factory(settings)

    def enqueue(
        self,
        *,
        subscription_id: str,
        message_id: str,
        resource: str,
        change_type: str,
        payload: dict[str, Any],
    ) -> bool:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO outlook_notifications (
                        subscription_id, message_id, resource, change_type, payload
                    ) VALUES (%s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (subscription_id, resource, change_type) DO NOTHING
                    RETURNING id
                    """,
                    (
                        subscription_id,
                        message_id,
                        resource,
                        change_type,
                        json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    ),
                )
                return cursor.fetchone() is not None

    def list(self, *, status: str = "pending", limit: int = 100) -> list[QueuedNotification]:
        if limit < 1 or limit > 500:
            raise ValueError("Notification limit must be between 1 and 500.")
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, subscription_id, message_id, resource, change_type,
                           received_at, status, attempts, last_error
                    FROM outlook_notifications
                    WHERE status = %s
                    ORDER BY received_at
                    LIMIT %s
                    """,
                    (status, limit),
                )
                rows = cursor.fetchall()
        return [self._notification(row) for row in rows]

    def claim_next(self) -> QueuedNotification | None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH next_notification AS (
                        SELECT id
                        FROM outlook_notifications
                        WHERE status = 'pending'
                        ORDER BY received_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE outlook_notifications AS notification
                    SET status = 'processing',
                        attempts = notification.attempts + 1,
                        last_error = NULL
                    FROM next_notification
                    WHERE notification.id = next_notification.id
                    RETURNING notification.id, notification.subscription_id,
                              notification.message_id, notification.resource,
                              notification.change_type, notification.received_at,
                              notification.status, notification.attempts,
                              notification.last_error
                    """
                )
                row = cursor.fetchone()
        return self._notification(row) if row is not None else None

    def mark_completed(self, notification_id: int) -> None:
        self._set_status(notification_id, "completed")

    def mark_failed(self, notification_id: int, error: str) -> None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE outlook_notifications
                    SET status = 'failed', last_error = %s
                    WHERE id = %s
                    """,
                    (error[:2000], notification_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(f"Notification {notification_id} was not found.")

    def retry_failed(self, notification_id: int) -> None:
        self._set_status(notification_id, "pending")

    def _set_status(self, notification_id: int, status: str) -> None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE outlook_notifications SET status = %s WHERE id = %s",
                    (status, notification_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(f"Notification {notification_id} was not found.")

    @staticmethod
    def _notification(row: dict[str, object]) -> QueuedNotification:
        received_at = row["received_at"]
        if isinstance(received_at, datetime):
            received_at = received_at.astimezone(timezone.utc).isoformat()
        return QueuedNotification(
            id=int(row["id"]),
            subscription_id=str(row["subscription_id"]),
            message_id=str(row["message_id"]),
            resource=str(row["resource"]),
            change_type=str(row["change_type"]),
            received_at=str(received_at),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        )

