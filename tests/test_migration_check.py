"""The app and worker refuse to start against a database that is missing a
migration (the SQL itself is verified on a real Postgres, see CLAUDE.md)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from app.postgres_migrate import pending_migrations, require_migrations_applied


class FakeCursor:
    def __init__(self, applied: list[str] | None) -> None:
        self.applied = applied
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: object = None) -> None:
        if "to_regclass" in sql:
            exists = self.applied is not None
            self.rows = [{"name": "schema_migrations" if exists else None}]
        else:
            self.rows = [{"version": name} for name in self.applied or []]

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def factory_for(applied: list[str] | None):
    class Connection:
        def cursor(self) -> FakeCursor:
            return FakeCursor(applied)

    @contextmanager
    def connect() -> Iterator[Connection]:
        yield Connection()

    return connect


def migrations(tmp_path: Path, *names: str) -> Path:
    for name in names:
        (tmp_path / name).write_text("SELECT 1;")
    return tmp_path


def test_missing_migration_is_named(tmp_path: Path) -> None:
    directory = migrations(tmp_path, "001_a.sql", "002_b.sql")

    assert pending_migrations(factory_for(["001_a.sql"]), directory) == ["002_b.sql"]
    with pytest.raises(RuntimeError, match="002_b.sql.*python -m app.postgres_migrate"):
        require_migrations_applied(factory_for(["001_a.sql"]), directory)


def test_fresh_database_has_every_migration_pending(tmp_path: Path) -> None:
    directory = migrations(tmp_path, "001_a.sql", "002_b.sql")

    assert pending_migrations(factory_for(None), directory) == ["001_a.sql", "002_b.sql"]


def test_up_to_date_database_starts(tmp_path: Path) -> None:
    directory = migrations(tmp_path, "001_a.sql")

    require_migrations_applied(factory_for(["001_a.sql"]), directory)
