from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.config_db import connect


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
    company: str
    supplier: str
    supplier_account_number: str | None = None
    default_payment_method: str | None = None
    payment_terms_notice: str | None = None
    bank_account: str | None = None


class SupplierTermsStore:
    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path

    def list(self) -> list[SupplierTerms]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM supplier_terms ORDER BY company, supplier"
            ).fetchall()
        return [_row_to_terms(row) for row in rows]

    def get(self, company: str, supplier: str) -> SupplierTerms | None:
        company_key = company.strip().casefold()
        supplier_key = supplier.strip().casefold()
        for terms in self.list():
            if (
                terms.company.strip().casefold() == company_key
                and terms.supplier.strip().casefold() == supplier_key
            ):
                return terms
        return None

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
            company=company,
            supplier=supplier,
            supplier_account_number=supplier_account_number,
            default_payment_method=default_payment_method,
            payment_terms_notice=payment_terms_notice,
            bank_account=bank_account,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO supplier_terms (
                    company, supplier, supplier_account_number,
                    default_payment_method, payment_terms_notice, bank_account
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(company, supplier) DO UPDATE SET
                    supplier_account_number = excluded.supplier_account_number,
                    default_payment_method = excluded.default_payment_method,
                    payment_terms_notice = excluded.payment_terms_notice,
                    bank_account = excluded.bank_account
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
            connection.commit()
        return terms

    def delete(self, company: str, supplier: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM supplier_terms WHERE company = ? AND supplier = ?",
                (company, supplier),
            )
            connection.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"No supplier terms found for '{company}' / '{supplier}'.")

    def _connect(self) -> sqlite3.Connection:
        return connect(self.database_path)


def _row_to_terms(row: sqlite3.Row) -> SupplierTerms:
    return SupplierTerms(
        company=row["company"],
        supplier=row["supplier"],
        supplier_account_number=row["supplier_account_number"],
        default_payment_method=row["default_payment_method"],
        payment_terms_notice=row["payment_terms_notice"],
        bank_account=row["bank_account"],
    )
