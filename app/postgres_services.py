from __future__ import annotations

from datetime import datetime, timezone

from app.activity_feed import ActivityEvent
from app.postgres_settings import (
    ConnectionFactory,
    PostgresSettings,
    postgres_connection_factory,
)


class PostgresIrjNumberGenerator:
    def __init__(
        self,
        settings: PostgresSettings | None = None,
        *,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if connection_factory is None:
            connection_factory = postgres_connection_factory(settings)
        self._connection_factory = connection_factory

    def generate(self, company: str | None = None) -> str:
        if company is not None:
            return self._generate_for_company(company)
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO irj_sequence (id, next_number)
                    VALUES (1, 2)
                    ON CONFLICT (id) DO UPDATE
                    SET next_number = irj_sequence.next_number + 1
                    RETURNING next_number - 1 AS number
                    """
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL did not return an IRJ sequence number.")
        number = int(row["number"])
        if number > 999999:
            raise RuntimeError("The six-digit IRJ number range is exhausted.")
        return f"{number:06d}"

    def reserve(self, irj_number: str, company: str | None = None) -> None:
        if company is not None:
            self._reserve_for_company(company, irj_number)
            return
        number = int(irj_number)
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO irj_sequence (id, next_number)
                    VALUES (1, %s)
                    ON CONFLICT (id) DO UPDATE
                    SET next_number = GREATEST(
                        irj_sequence.next_number,
                        excluded.next_number
                    )
                    """,
                    (number + 1,),
                )

    def current(self, company: str) -> str | None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT next_number FROM company_irj_sequences
                    WHERE lower(company) = lower(%s)
                    """,
                    (company.strip(),),
                )
                row = cursor.fetchone()
        if row is None or int(row["next_number"]) <= 1:
            return None
        return f"{int(row['next_number']) - 1:06d}"

    def set_current(self, company: str, irj_number: str) -> None:
        number = self._validate_number(irj_number)
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO company_irj_sequences (company, next_number)
                    VALUES (%s, %s)
                    ON CONFLICT (company) DO UPDATE
                    SET next_number = excluded.next_number
                    """,
                    (company.strip(), number + 1),
                )

    def _generate_for_company(self, company: str) -> str:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO company_irj_sequences (company, next_number)
                    VALUES (%s, 2)
                    ON CONFLICT (company) DO UPDATE
                    SET next_number = company_irj_sequences.next_number + 1
                    RETURNING next_number - 1 AS number
                    """,
                    (company.strip(),),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL did not return an IRJ sequence number.")
        number = int(row["number"])
        if number > 999999:
            raise RuntimeError(
                f"The six-digit IRJ number range for {company} is exhausted."
            )
        return f"{number:06d}"

    def _reserve_for_company(self, company: str, irj_number: str) -> None:
        number = self._validate_number(irj_number)
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO company_irj_sequences (company, next_number)
                    VALUES (%s, %s)
                    ON CONFLICT (company) DO UPDATE
                    SET next_number = GREATEST(
                        company_irj_sequences.next_number,
                        excluded.next_number
                    )
                    """,
                    (company.strip(), number + 1),
                )

    @staticmethod
    def _validate_number(irj_number: str) -> int:
        normalized = irj_number.strip()
        if len(normalized) != 6 or not normalized.isdigit():
            raise ValueError("The IRJ number must contain exactly six digits.")
        return int(normalized)


class PostgresActivityFeedStore:
    def __init__(
        self,
        settings: PostgresSettings | None = None,
        *,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        if connection_factory is None:
            connection_factory = postgres_connection_factory(settings)
        self._connection_factory = connection_factory

    def add_event(
        self,
        *,
        event_type: str,
        target_role: str,
        message: str,
        invoice_id: int | None = None,
    ) -> ActivityEvent:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO activity_events (
                        event_type, target_role, message, invoice_id
                    ) VALUES (%s, %s, %s, %s)
                    RETURNING id, event_type, target_role, message,
                              invoice_id, created_at
                    """,
                    (event_type, target_role, message, invoice_id),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL did not return the activity event.")
        return _activity_event(row)

    def list_since(
        self, since_id: int = 0, limit: int = 50
    ) -> list[ActivityEvent]:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, event_type, target_role, message,
                           invoice_id, created_at
                    FROM activity_events
                    WHERE id > %s
                    ORDER BY id ASC
                    LIMIT %s
                    """,
                    (since_id, limit),
                )
                rows = cursor.fetchall()
        return [_activity_event(row) for row in rows]

    def latest_id(self) -> int:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COALESCE(MAX(id), 0) AS latest FROM activity_events"
                )
                row = cursor.fetchone()
        return int(row["latest"]) if row is not None else 0


def _activity_event(row: dict[str, object]) -> ActivityEvent:
    created_at = row["created_at"]
    if isinstance(created_at, datetime):
        timestamp = created_at.astimezone(timezone.utc).isoformat()
    else:
        timestamp = str(created_at)
    return ActivityEvent(
        id=int(row["id"]),
        event_type=str(row["event_type"]),
        target_role=str(row["target_role"]),
        message=str(row["message"]),
        invoice_id=int(row["invoice_id"]) if row["invoice_id"] is not None else None,
        created_at=timestamp,
    )
