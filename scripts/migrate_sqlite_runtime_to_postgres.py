"""One-time, idempotent migration of legacy local runtime data to PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from app.environment import load_project_environment
from app.invoices import InvoiceRecord
from app.postgres_config_migrate import migrate_sqlite_configuration
from app.postgres_invoices import PostgresInvoiceStore
from app.postgres_settings import PostgresSettings, postgres_connection_factory


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime_data"
INVOICE_COLUMNS = tuple(InvoiceRecord.__dataclass_fields__)


def _rows(path: Path, table: str) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


def _migrate_invoices(factory) -> tuple[int, dict[int, int]]:
    rows = _rows(RUNTIME / "invoices.db", "invoices")
    if not rows:
        return 0, {}
    columns = [column for column in INVOICE_COLUMNS if column != "duplicate_of_invoice_id"]
    assignments = ", ".join(
        f"{column} = excluded.{column}" for column in columns if column != "id"
    )
    id_map: dict[int, int] = {}
    with factory() as connection:
        with connection.cursor() as cursor:
            for row in rows:
                cursor.execute(
                    f"""
                    INSERT INTO invoices ({', '.join(columns)})
                    VALUES ({', '.join(['%s'] * len(columns))})
                    ON CONFLICT (message_id, attachment_id) DO UPDATE SET {assignments}
                    RETURNING id
                    """,
                    tuple(row.get(column) for column in columns),
                )
                migrated = cursor.fetchone()
                id_map[int(row["id"])] = int(migrated["id"])
            for row in rows:
                duplicate_id = row.get("duplicate_of_invoice_id")
                if duplicate_id is not None and int(duplicate_id) in id_map:
                    cursor.execute(
                        "UPDATE invoices SET duplicate_of_invoice_id = %s WHERE id = %s",
                        (id_map[int(duplicate_id)], id_map[int(row["id"])]),
                    )
            cursor.execute(
                "SELECT setval(pg_get_serial_sequence('invoices', 'id'), "
                "GREATEST((SELECT COALESCE(MAX(id), 1) FROM invoices), 1), true)"
            )
    return len(rows), id_map


def _migrate_irj(factory) -> int:
    global_rows = _rows(RUNTIME / "invoices.db", "irj_sequence")
    company_rows = _rows(RUNTIME / "invoices.db", "company_irj_sequences")
    with factory() as connection:
        with connection.cursor() as cursor:
            for row in global_rows:
                cursor.execute(
                    """
                    INSERT INTO irj_sequence (id, next_number) VALUES (%s, %s)
                    ON CONFLICT (id) DO UPDATE
                    SET next_number = GREATEST(irj_sequence.next_number, excluded.next_number)
                    """,
                    (row["id"], row["next_number"]),
                )
            for row in company_rows:
                cursor.execute(
                    """
                    INSERT INTO company_irj_sequences (company, next_number)
                    VALUES (%s, %s)
                    ON CONFLICT (company) DO UPDATE
                    SET next_number = GREATEST(
                        company_irj_sequences.next_number, excluded.next_number
                    )
                    """,
                    (row["company"], row["next_number"]),
                )
    return len(global_rows) + len(company_rows)


def _migrate_activity(factory, invoice_ids: dict[int, int]) -> int:
    events = _rows(RUNTIME / "activity_feed.db", "activity_events")
    stages = _rows(RUNTIME / "activity_feed.db", "invoice_email_stages")
    with factory() as connection:
        with connection.cursor() as cursor:
            for row in events:
                invoice_id = row.get("invoice_id")
                mapped_id = invoice_ids.get(int(invoice_id)) if invoice_id is not None else None
                cursor.execute(
                    """
                    INSERT INTO activity_events (
                        id, event_type, target_role, message, invoice_id, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        event_type = excluded.event_type,
                        target_role = excluded.target_role,
                        message = excluded.message,
                        invoice_id = excluded.invoice_id,
                        created_at = excluded.created_at
                    """,
                    (
                        row["id"], row["event_type"], row["target_role"],
                        row["message"], mapped_id, row["created_at"],
                    ),
                )
            for row in stages:
                mapped_id = invoice_ids.get(int(row["invoice_id"]))
                if mapped_id is not None:
                    cursor.execute(
                        """
                        INSERT INTO invoice_email_stages (invoice_id, stage, claimed_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (invoice_id, stage) DO NOTHING
                        """,
                        (mapped_id, row["stage"], row["claimed_at"]),
                    )
            cursor.execute(
                "SELECT setval(pg_get_serial_sequence('activity_events', 'id'), "
                "GREATEST((SELECT COALESCE(MAX(id), 1) FROM activity_events), 1), true)"
            )
    return len(events) + len(stages)


def _migrate_auth(factory) -> int:
    sessions = _rows(RUNTIME / "auth.db", "sessions")
    flows = _rows(RUNTIME / "auth.db", "oauth_flows")
    with factory() as connection:
        with connection.cursor() as cursor:
            for row in sessions:
                cursor.execute(
                    """
                    INSERT INTO auth_sessions (
                        token_hash, username, display_name, email, role,
                        created_at, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (token_hash) DO UPDATE SET
                        username = excluded.username,
                        display_name = excluded.display_name,
                        email = excluded.email,
                        role = excluded.role,
                        expires_at = excluded.expires_at
                    """,
                    (
                        hashlib.sha256(str(row["token"]).encode()).hexdigest(),
                        row["username"], row["display_name"], row["email"],
                        row["role"], row["created_at"], row["expires_at"],
                    ),
                )
            for row in flows:
                cursor.execute(
                    """
                    INSERT INTO oauth_flows (state, flow_json, expires_at)
                    VALUES (%s, %s::jsonb, %s)
                    ON CONFLICT (state) DO UPDATE SET
                        flow_json = excluded.flow_json,
                        expires_at = excluded.expires_at
                    """,
                    (row["state"], row["flow_json"], row["expires_at"]),
                )
    return len(sessions) + len(flows)


def _migrate_notifications(factory) -> int:
    rows = _rows(RUNTIME / "outlook_notifications.db", "outlook_notifications")
    with factory() as connection:
        with connection.cursor() as cursor:
            for row in rows:
                payload = row["payload"]
                json.loads(str(payload))
                cursor.execute(
                    """
                    INSERT INTO outlook_notifications (
                        subscription_id, message_id, resource, change_type,
                        received_at, status, attempts, last_error, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (subscription_id, resource, change_type) DO UPDATE SET
                        message_id = excluded.message_id,
                        received_at = excluded.received_at,
                        status = excluded.status,
                        attempts = excluded.attempts,
                        last_error = excluded.last_error,
                        payload = excluded.payload
                    """,
                    (
                        row["subscription_id"], row["message_id"], row["resource"],
                        row["change_type"], row["received_at"], row["status"],
                        row["attempts"], row["last_error"], payload,
                    ),
                )
    return len(rows)


def main() -> None:
    load_project_environment()
    settings = PostgresSettings.from_env()
    factory = postgres_connection_factory(settings)
    PostgresInvoiceStore(settings, initialize_schema=True)
    config_counts = migrate_sqlite_configuration(
        RUNTIME / "config.db", connection_factory=factory
    )
    invoice_count, invoice_ids = _migrate_invoices(factory)
    counts = {
        **config_counts,
        "invoices": invoice_count,
        "irj_sequences": _migrate_irj(factory),
        "activity": _migrate_activity(factory, invoice_ids),
        "auth": _migrate_auth(factory),
        "outlook_notifications": _migrate_notifications(factory),
    }
    print("PostgreSQL migration complete: " + ", ".join(
        f"{name}={count}" for name, count in counts.items()
    ))


if __name__ == "__main__":
    main()
