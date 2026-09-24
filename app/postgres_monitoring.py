from __future__ import annotations

import json
from datetime import datetime, timezone

from app.postgres_settings import ConnectionFactory, PostgresSettings, postgres_connection_factory


class PostgresWorkerMonitor:
    def __init__(
        self,
        settings: PostgresSettings | None = None,
        *,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._connection_factory = connection_factory or postgres_connection_factory(settings)

    def heartbeat(
        self,
        worker_name: str,
        *,
        status: str,
        success: bool = False,
        error: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO worker_heartbeats (
                        worker_name, status, last_seen_at, last_success_at,
                        last_error, details
                    ) VALUES (%s, %s, now(), CASE WHEN %s THEN now() END, %s, %s::jsonb)
                    ON CONFLICT (worker_name) DO UPDATE SET
                        status = excluded.status,
                        last_seen_at = now(),
                        last_success_at = CASE WHEN %s THEN now()
                            ELSE worker_heartbeats.last_success_at END,
                        last_error = excluded.last_error,
                        details = excluded.details
                    """,
                    (
                        worker_name,
                        status,
                        success,
                        error[:2000] if error else None,
                        json.dumps(details or {}, separators=(",", ":")),
                        success,
                    ),
                )

    def status(self, worker_name: str = "outlook-invoice-worker") -> dict[str, object] | None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT worker_name, status, last_seen_at, last_success_at,
                           last_error, details,
                           EXTRACT(EPOCH FROM (now() - last_seen_at))::integer AS age_seconds
                    FROM worker_heartbeats WHERE worker_name = %s
                    """,
                    (worker_name,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return {
            **row,
            "last_seen_at": self._timestamp(row["last_seen_at"]),
            "last_success_at": self._timestamp(row["last_success_at"]),
        }

    @staticmethod
    def _timestamp(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        return str(value)
