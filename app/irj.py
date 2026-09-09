from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS irj_sequence (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_number INTEGER NOT NULL DEFAULT 1
);
"""


class IrjNumberGenerator:
    """Generates unique, sequential internal invoice reference numbers
    (IRJ numbers), e.g. 000123.

    Uses the same SQLite database as invoice records so a single connection
    file can guarantee atomic, non-duplicated numbering across workers.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO irj_sequence (id, next_number) VALUES (1, 1)"
            )
            connection.commit()

    def generate(self) -> str:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT next_number FROM irj_sequence WHERE id = 1"
            ).fetchone()
            number = int(row["next_number"])
            if number > 999999:
                raise RuntimeError("The six-digit IRJ number range is exhausted.")
            connection.execute(
                "UPDATE irj_sequence SET next_number = ? WHERE id = 1",
                (number + 1,),
            )
            connection.commit()
        return f"{number:06d}"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection
