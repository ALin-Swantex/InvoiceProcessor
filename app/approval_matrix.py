from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.config_db import connect

ALL_COMPANIES = "*"


# ---------------------------------------------------------------------------
# Approval Matrix — admin-maintained routing rules mapping (company,
# supplier) to Approver 1 and an optional Approver 2. SQLite-backed today;
# see app/config_db.py docstring for the intended SharePoint List swap.
#
# If a supplier is not on the matrix for the invoiced company, the invoice
# must be flagged for manual review rather than automatically sent to
# somebody (SOFTWARE_SPEC.md section 7) -- see InvoiceLifecycle.confirm_and_route.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Approver:
    name: str
    email: str


@dataclass(frozen=True)
class ApprovalMatrixEntry:
    id: int
    company: str
    supplier: str
    approver1: Approver
    approver2: Approver | None = None


_DEFAULT_ENTRIES: list[tuple[str, str, Approver, Approver | None]] = [
    (
        "Acme Trading Ltd",
        "Supplier Ltd",
        Approver(name="Jordan Blake", email="jordan.blake@example.test"),
        Approver(name="Sam Ellis", email="sam.ellis@example.test"),
    ),
    (
        "Acme Trading Ltd",
        "Northgate Supplies",
        Approver(name="Jordan Blake", email="jordan.blake@example.test"),
        None,
    ),
    (
        "Northfield Manufacturing",
        "Supplier Ltd",
        Approver(name="Priya Nair", email="priya.nair@example.test"),
        Approver(name="Chris Adeyemi", email="chris.adeyemi@example.test"),
    ),
    (
        "Riverside Logistics",
        "Northgate Supplies",
        Approver(name="Morgan Reyes", email="morgan.reyes@example.test"),
        None,
    ),
]


class ApprovalMatrixStore:
    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT COUNT(*) AS n FROM approval_matrix"
            ).fetchone()
            if not existing["n"]:
                for company, supplier, approver1, approver2 in _DEFAULT_ENTRIES:
                    self._insert(connection, company, supplier, approver1, approver2)
                connection.commit()

    def list(self) -> list[ApprovalMatrixEntry]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM approval_matrix ORDER BY company, supplier"
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def find(self, company: str, supplier: str) -> ApprovalMatrixEntry | None:
        company_key = company.strip().casefold()
        supplier_key = supplier.strip().casefold()
        fallback: ApprovalMatrixEntry | None = None
        for entry in self.list():
            if entry.supplier.strip().casefold() != supplier_key:
                continue
            if entry.company.strip().casefold() == company_key:
                return entry
            if entry.company == ALL_COMPANIES:
                fallback = entry
        return fallback

    def find_exact(self, company: str, supplier: str) -> ApprovalMatrixEntry | None:
        company_key = company.strip().casefold()
        supplier_key = supplier.strip().casefold()
        return next(
            (
                entry
                for entry in self.list()
                if entry.company.strip().casefold() == company_key
                and entry.supplier.strip().casefold() == supplier_key
            ),
            None,
        )

    def create(
        self,
        *,
        company: str,
        supplier: str,
        approver1_name: str,
        approver1_email: str,
        approver2_name: str | None = None,
        approver2_email: str | None = None,
    ) -> ApprovalMatrixEntry:
        approver1 = Approver(name=approver1_name, email=approver1_email)
        approver2 = (
            Approver(name=approver2_name, email=approver2_email or "")
            if approver2_name
            else None
        )
        with self._connect() as connection:
            try:
                cursor = self._insert(connection, company, supplier, approver1, approver2)
                connection.commit()
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    f"An approval matrix entry for '{company}' / '{supplier}' already exists."
                ) from error
            entry_id = cursor.lastrowid
        return ApprovalMatrixEntry(
            id=entry_id, company=company, supplier=supplier, approver1=approver1, approver2=approver2
        )

    def update(self, entry_id: int, **fields: object) -> ApprovalMatrixEntry:
        allowed = {
            "company",
            "supplier",
            "approver1_name",
            "approver1_email",
            "approver2_name",
            "approver2_email",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown approval matrix fields: {', '.join(sorted(unknown))}.")
        if not fields:
            existing = self._get_by_id(entry_id)
            if existing is None:
                raise KeyError(f"Approval matrix entry {entry_id} was not found.")
            return existing
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    f"UPDATE approval_matrix SET {assignments} WHERE id = ?",
                    (*fields.values(), entry_id),
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    "An approval matrix entry for that company and supplier "
                    "already exists."
                ) from error
            if cursor.rowcount == 0:
                raise KeyError(f"Approval matrix entry {entry_id} was not found.")
        result = self._get_by_id(entry_id)
        if result is None:
            raise RuntimeError(
                f"Approval matrix entry {entry_id} disappeared after it was updated."
            )
        return result

    def delete(self, entry_id: int) -> None:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM approval_matrix WHERE id = ?", (entry_id,))
            connection.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"Approval matrix entry {entry_id} was not found.")

    def delete_by_supplier(self, supplier: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM approval_matrix WHERE supplier = ?", (supplier,)
            )
            connection.commit()
            return cursor.rowcount

    def delete_by_company(self, company: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM approval_matrix WHERE company = ?", (company,)
            )
            connection.commit()
            return cursor.rowcount

    def _get_by_id(self, entry_id: int) -> ApprovalMatrixEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approval_matrix WHERE id = ?", (entry_id,)
            ).fetchone()
        return _row_to_entry(row) if row is not None else None

    def _insert(
        self,
        connection: sqlite3.Connection,
        company: str,
        supplier: str,
        approver1: Approver,
        approver2: Approver | None,
    ) -> sqlite3.Cursor:
        return connection.execute(
            """
            INSERT INTO approval_matrix (
                company, supplier, approver1_name, approver1_email,
                approver2_name, approver2_email
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                company,
                supplier,
                approver1.name,
                approver1.email,
                approver2.name if approver2 else None,
                approver2.email if approver2 else None,
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect(self.database_path)


def _row_to_entry(row: sqlite3.Row) -> ApprovalMatrixEntry:
    approver2 = (
        Approver(name=row["approver2_name"], email=row["approver2_email"] or "")
        if row["approver2_name"]
        else None
    )
    return ApprovalMatrixEntry(
        id=row["id"],
        company=row["company"],
        supplier=row["supplier"],
        approver1=Approver(name=row["approver1_name"], email=row["approver1_email"]),
        approver2=approver2,
    )


_default_store: ApprovalMatrixStore | None = None


def _store() -> ApprovalMatrixStore:
    global _default_store
    if _default_store is None:
        _default_store = ApprovalMatrixStore()
    return _default_store


def list_matrix() -> list[ApprovalMatrixEntry]:
    return _store().list()


def find_approvers(company: str, supplier: str) -> ApprovalMatrixEntry | None:
    return _store().find(company, supplier)
