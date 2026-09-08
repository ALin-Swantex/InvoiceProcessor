from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.config_db import connect
from app.approval_matrix import ALL_COMPANIES


# ---------------------------------------------------------------------------
# Supplier Terms — admin-maintained payment configuration per (company,
# supplier) pair: the supplier's account number, the default payment method,
# a free-text payment terms notice, and which bank account to pay from.
#
# This is keyed the same way as the Approval Matrix (company, supplier)
# rather than just supplier, because the same supplier can be paid
# differently (different bank account, different terms) depending on which
# of your companies is being invoiced -- mirrors app/approval_matrix.py.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupplierTerms:
    id: int
    company: str
    supplier: str
    supplier_account_number: str | None = None
    default_payment_method: str | None = None
    payment_terms_notice: str | None = None
    bank_account: str | None = None


class SupplierTermsStore:
    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path
        self._migrate_legacy_schema()

    def list(self) -> list[SupplierTerms]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM supplier_terms ORDER BY company, supplier"
            ).fetchall()
        return [_row_to_terms(row) for row in rows]

    def get(self, company: str, supplier: str) -> SupplierTerms | None:
        profiles = self.list_for_supplier(company, supplier)
        return profiles[0] if profiles else None

    def list_for_supplier(self, company: str, supplier: str) -> list[SupplierTerms]:
        company_key = company.strip().casefold()
        supplier_key = supplier.strip().casefold()
        matching = [
            terms
            for terms in self.list()
            if (
                terms.supplier.strip().casefold() == supplier_key
                and terms.company.strip().casefold() in (company_key, ALL_COMPANIES)
            )
        ]
        company_specific = [
            terms
            for terms in matching
            if terms.company.strip().casefold() == company_key
        ]
        return company_specific or [
            terms for terms in matching if terms.company == ALL_COMPANIES
        ]

    def upsert(
        self,
        *,
        company: str,
        supplier: str,
        supplier_account_number: str | None = None,
        default_payment_method: str | None = None,
        payment_terms_notice: str | None = None,
        bank_account: str | None = None,
    ) -> SupplierTerms:
        terms = SupplierTerms(
            id=0,
            company=company,
            supplier=supplier,
            supplier_account_number=supplier_account_number,
            default_payment_method=default_payment_method,
            payment_terms_notice=payment_terms_notice,
            bank_account=bank_account,
        )
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM supplier_terms
                WHERE company = ? AND (
                    supplier_account_number = ?
                    OR (supplier_account_number IS NULL AND ? IS NULL AND supplier = ?)
                )
                """,
                (
                    company,
                    supplier_account_number,
                    supplier_account_number,
                    supplier,
                ),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO supplier_terms (
                        company, supplier, supplier_account_number,
                        default_payment_method, payment_terms_notice, bank_account
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        company,
                        supplier,
                        supplier_account_number,
                        default_payment_method,
                        payment_terms_notice,
                        bank_account,
                    ),
                )
                terms = SupplierTerms(**{**terms.__dict__, "id": cursor.lastrowid})
            else:
                connection.execute(
                    """
                    UPDATE supplier_terms
                    SET supplier = ?, default_payment_method = ?,
                        payment_terms_notice = ?, bank_account = ?
                    WHERE id = ?
                    """,
                    (
                        supplier,
                        default_payment_method,
                        payment_terms_notice,
                        bank_account,
                        existing["id"],
                    ),
                )
                terms = SupplierTerms(**{**terms.__dict__, "id": existing["id"]})
            connection.commit()
        return terms

    def update(self, terms_id: int, **fields: object) -> SupplierTerms:
        allowed = {
            "company",
            "supplier",
            "supplier_account_number",
            "default_payment_method",
            "payment_terms_notice",
            "bank_account",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(
                f"Unknown supplier terms fields: {', '.join(sorted(unknown))}."
            )
        if not fields:
            existing = self._get_by_id(terms_id)
            if existing is None:
                raise KeyError(f"Supplier terms entry {terms_id} was not found.")
            return existing
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    f"UPDATE supplier_terms SET {assignments} WHERE id = ?",
                    (*fields.values(), terms_id),
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    "A payment profile with that company and account number "
                    "already exists."
                ) from error
            if cursor.rowcount == 0:
                raise KeyError(f"Supplier terms entry {terms_id} was not found.")
        result = self._get_by_id(terms_id)
        if result is None:
            raise RuntimeError(
                f"Supplier terms entry {terms_id} disappeared after it was updated."
            )
        return result

    def delete(self, company: str, supplier: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM supplier_terms WHERE company = ? AND supplier = ?",
                (company, supplier),
            )
            connection.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"No supplier terms found for '{company}' / '{supplier}'.")

    def delete_by_supplier(self, supplier: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM supplier_terms WHERE supplier = ?", (supplier,)
            )
            connection.commit()
            return cursor.rowcount

    def delete_by_company(self, company: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM supplier_terms WHERE company = ?", (company,)
            )
            connection.commit()
            return cursor.rowcount

    def _connect(self) -> sqlite3.Connection:
        return connect(self.database_path)

    def _get_by_id(self, terms_id: int) -> SupplierTerms | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM supplier_terms WHERE id = ?", (terms_id,)
            ).fetchone()
        return _row_to_terms(row) if row is not None else None

    def _migrate_legacy_schema(self) -> None:
        with self._connect() as connection:
            columns = connection.execute("PRAGMA table_info(supplier_terms)").fetchall()
            primary_keys = [row["name"] for row in columns if row["pk"]]
            if primary_keys == ["id"]:
                return
            connection.execute("ALTER TABLE supplier_terms RENAME TO supplier_terms_legacy")
            connection.execute(
                """
                CREATE TABLE supplier_terms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company TEXT NOT NULL,
                    supplier TEXT NOT NULL,
                    supplier_account_number TEXT,
                    default_payment_method TEXT,
                    payment_terms_notice TEXT,
                    bank_account TEXT,
                    UNIQUE(company, supplier_account_number)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO supplier_terms (
                    company, supplier, supplier_account_number,
                    default_payment_method, payment_terms_notice, bank_account
                )
                SELECT company, supplier, supplier_account_number,
                       default_payment_method, payment_terms_notice, bank_account
                FROM supplier_terms_legacy
                """
            )
            connection.execute("DROP TABLE supplier_terms_legacy")
            connection.commit()


def _row_to_terms(row: sqlite3.Row) -> SupplierTerms:
    return SupplierTerms(
        id=row["id"],
        company=row["company"],
        supplier=row["supplier"],
        supplier_account_number=row["supplier_account_number"],
        default_payment_method=row["default_payment_method"],
        payment_terms_notice=row["payment_terms_notice"],
        bank_account=row["bank_account"],
    )
