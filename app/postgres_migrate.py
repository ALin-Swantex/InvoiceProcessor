from __future__ import annotations

import hashlib
from pathlib import Path

from app.environment import load_project_environment
from app.postgres_settings import (
    ConnectionFactory,
    PostgresSettings,
    close_postgres_pools,
    postgres_connection_factory,
)


MIGRATIONS_DIRECTORY = Path(__file__).with_name("postgres_migrations")


def apply_migrations(
    connection_factory: ConnectionFactory,
    migrations_directory: Path = MIGRATIONS_DIRECTORY,
) -> None:
    """Apply every migration not yet recorded in schema_migrations, refusing
    to start if an applied migration's file has changed."""
    migrations = sorted(migrations_directory.glob("*.sql"))
    if not migrations:
        raise RuntimeError("No PostgreSQL schema migrations were found.")
    with connection_factory() as connection:
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


def main() -> None:
    load_project_environment()
    try:
        apply_migrations(postgres_connection_factory(PostgresSettings.from_env()))
        print("PostgreSQL migrations applied successfully.")
    finally:
        # Close the pool so the command exits immediately.
        close_postgres_pools()


if __name__ == "__main__":
    main()
