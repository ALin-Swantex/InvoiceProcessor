from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS irj_sequence (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_number INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS company_irj_sequences (
    company TEXT PRIMARY KEY COLLATE NOCASE,
    next_number INTEGER NOT NULL CHECK (next_number > 0)
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

    def generate(self, company: str | None = None) -> str:
        if company is not None:
            return self._generate_for_company(company)
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

    def reserve(self, irj_number: str, company: str | None = None) -> None:
        if company is not None:
            self._reserve_for_company(company, irj_number)
            return
        number = int(irj_number)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE irj_sequence
                SET next_number = MAX(next_number, ?)
                WHERE id = 1
                """,
                (number + 1,),
            )
            connection.commit()

    def current(self, company: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT next_number
                FROM company_irj_sequences
                WHERE company = ? COLLATE NOCASE
                """,
                (company.strip(),),
            ).fetchone()
        if row is None or int(row["next_number"]) <= 1:
            return None
        return f"{int(row['next_number']) - 1:06d}"

    def set_current(self, company: str, irj_number: str) -> None:
        number = self._validate_number(irj_number)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO company_irj_sequences (company, next_number)
                VALUES (?, ?)
                ON CONFLICT(company) DO UPDATE SET next_number = excluded.next_number
                """,
                (company.strip(), number + 1),
            )
            connection.commit()

    def _generate_for_company(self, company: str) -> str:
        company = company.strip()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO company_irj_sequences (company, next_number)
                VALUES (?, 1)
                """,
                (company,),
            )
            row = connection.execute(
                """
                SELECT next_number FROM company_irj_sequences
                WHERE company = ? COLLATE NOCASE
                """,
                (company,),
            ).fetchone()
            number = int(row["next_number"])
            if number > 999999:
                raise RuntimeError(
                    f"The six-digit IRJ number range for {company} is exhausted."
                )
            connection.execute(
                """
                UPDATE company_irj_sequences
                SET next_number = ?
                WHERE company = ? COLLATE NOCASE
                """,
                (number + 1, company),
            )
            connection.commit()
        return f"{number:06d}"

    def _reserve_for_company(self, company: str, irj_number: str) -> None:
        number = self._validate_number(irj_number)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO company_irj_sequences (company, next_number)
                VALUES (?, ?)
                ON CONFLICT(company) DO UPDATE
                SET next_number = MAX(next_number, excluded.next_number)
                """,
                (company.strip(), number + 1),
            )
            connection.commit()

    @staticmethod
    def _validate_number(irj_number: str) -> int:
        normalized = irj_number.strip()
        if len(normalized) != 6 or not normalized.isdigit():
            raise ValueError("The IRJ number must contain exactly six digits.")
        return int(normalized)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection
