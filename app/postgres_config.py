from __future__ import annotations

import json
from typing import Iterable

from app.approval_matrix import (
    ALL_COMPANIES,
    ApprovalMatrixEntry,
    Approver,
)
from app.companies import CompanyProfile
from app.company_folders import CompanyFolderStructure
from app.invoice_number_validation import validate_invoice_number_pattern
from app.postgres_settings import (
    ConnectionFactory,
    PostgresSettings,
    postgres_connection_factory,
)
from app.supplier_terms import SupplierTerms
from app.suppliers import SupplierProfile, _normalize_aliases


class _PostgresStore:
    def __init__(
        self,
        settings: PostgresSettings | None = None,
        *,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._connection_factory = connection_factory or postgres_connection_factory(
            settings
        )


class PostgresCompanyStore(_PostgresStore):
    def list(self) -> list[CompanyProfile]:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT name, sharepoint_root_folder, company_folder,
                           po_matching_folder, aliases, vat_number, address
                    FROM companies
                    WHERE active
                    ORDER BY name
                    """
                )
                rows = cursor.fetchall()
        return [_company(row) for row in rows]

    def get(self, name: str) -> CompanyProfile | None:
        return _match_named(self.list(), name)

    def create(
        self,
        *,
        name: str,
        company_folder: str | None = None,
        po_matching_folder: str | None = None,
        sharepoint_root_folder: str | None = None,
        aliases: list[str] | None = None,
        vat_number: str | None = None,
        address: str | None = None,
    ) -> CompanyProfile:
        root = sharepoint_root_folder or company_folder
        if not root:
            raise ValueError("A SharePoint company root folder is required.")
        structure = CompanyFolderStructure.from_root(root)
        profile = CompanyProfile(
            name=name,
            sharepoint_root_folder=structure.root,
            company_folder=structure.nominal_invoices,
            po_matching_folder=structure.po_match,
            aliases=tuple(aliases or []),
            vat_number=vat_number,
            address=address,
        )
        try:
            with self._connection_factory() as connection:  # type: ignore[attr-defined]
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO companies (
                            name, sharepoint_root_folder, company_folder,
                            po_matching_folder, aliases, vat_number, address
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            profile.name,
                            profile.sharepoint_root_folder,
                            profile.company_folder,
                            profile.po_matching_folder,
                            ",".join(profile.aliases),
                            profile.vat_number,
                            profile.address,
                        ),
                    )
        except Exception as error:
            if _is_unique_violation(error):
                raise ValueError(f"Company '{name}' already exists.") from error
            raise
        return profile

    def update(self, name: str, **fields: object) -> CompanyProfile:
        allowed = {
            "sharepoint_root_folder",
            "company_folder",
            "po_matching_folder",
            "aliases",
            "vat_number",
            "address",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown company fields: {', '.join(sorted(unknown))}.")
        existing = self.get(name)
        if existing is None:
            raise KeyError(f"Company '{name}' was not found.")
        if "aliases" in fields and isinstance(fields["aliases"], (list, tuple)):
            fields["aliases"] = ",".join(str(value) for value in fields["aliases"])
        if not fields:
            return existing
        assignments = ", ".join(f"{key} = %s" for key in fields)
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE companies
                    SET {assignments}, updated_at = now()
                    WHERE lower(name) = lower(%s) AND active
                    """,
                    (*fields.values(), existing.name),
                )
                if cursor.rowcount == 0:
                    raise KeyError(f"Company '{name}' was not found.")
        updated = self.get(existing.name)
        if updated is None:
            raise RuntimeError(f"Company '{name}' disappeared after it was updated.")
        return updated

    def delete(self, name: str) -> None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM companies WHERE lower(name) = lower(%s)",
                    (name,),
                )
                if cursor.rowcount == 0:
                    raise KeyError(f"Company '{name}' was not found.")


class PostgresSupplierStore(_PostgresStore):
    def list(self) -> list[SupplierProfile]:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT name, aliases, default_company, contact_email,
                           invoice_number_pattern
                    FROM suppliers
                    WHERE active
                    ORDER BY name
                    """
                )
                rows = cursor.fetchall()
        return [_supplier(row) for row in rows]

    def get(self, name: str) -> SupplierProfile | None:
        return _match_named(self.list(), name)

    def create(
        self,
        *,
        name: str,
        aliases: list[str] | None = None,
        default_company: str | None = None,
        contact_email: str | None = None,
        invoice_number_pattern: str | None = None,
    ) -> SupplierProfile:
        normalized_name = name.strip()
        normalized_aliases = _normalize_aliases(aliases or [])
        self._ensure_identifiers_available(normalized_name, normalized_aliases)
        profile = SupplierProfile(
            name=normalized_name,
            aliases=normalized_aliases,
            default_company=default_company,
            contact_email=contact_email,
            invoice_number_pattern=validate_invoice_number_pattern(
                invoice_number_pattern
            ),
        )
        company_id = self._company_id(profile.default_company)
        try:
            with self._connection_factory() as connection:  # type: ignore[attr-defined]
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO suppliers (
                            name, aliases, default_company_id, default_company,
                            contact_email, invoice_number_pattern
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            profile.name,
                            ",".join(profile.aliases),
                            company_id,
                            profile.default_company,
                            profile.contact_email,
                            profile.invoice_number_pattern,
                        ),
                    )
        except Exception as error:
            if _is_unique_violation(error):
                raise ValueError(f"Supplier '{name}' already exists.") from error
            raise
        return profile

    def update(self, name: str, **fields: object) -> SupplierProfile:
        existing = self.get(name)
        if existing is None:
            raise KeyError(f"Supplier '{name}' was not found.")
        allowed = {
            "aliases",
            "default_company",
            "contact_email",
            "invoice_number_pattern",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown supplier fields: {', '.join(sorted(unknown))}.")
        if "aliases" in fields and isinstance(fields["aliases"], (list, tuple)):
            aliases = _normalize_aliases(fields["aliases"])
            self._ensure_identifiers_available(
                existing.name, aliases, excluding_name=existing.name
            )
            fields["aliases"] = ",".join(aliases)
        if "invoice_number_pattern" in fields:
            fields["invoice_number_pattern"] = validate_invoice_number_pattern(
                fields["invoice_number_pattern"]
                if isinstance(fields["invoice_number_pattern"], str)
                else None
            )
        if "default_company" in fields:
            fields["default_company_id"] = self._company_id(
                fields["default_company"]
                if isinstance(fields["default_company"], str)
                else None
            )
        if not fields:
            return existing
        assignments = ", ".join(f"{key} = %s" for key in fields)
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE suppliers
                    SET {assignments}, updated_at = now()
                    WHERE lower(name) = lower(%s) AND active
                    """,
                    (*fields.values(), existing.name),
                )
                if cursor.rowcount == 0:
                    raise KeyError(f"Supplier '{name}' was not found.")
        updated = self.get(existing.name)
        if updated is None:
            raise RuntimeError(f"Supplier '{name}' disappeared after it was updated.")
        return updated

    def _company_id(self, company: str | None) -> int | None:
        if not company:
            return None
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id FROM companies
                    WHERE lower(name) = lower(%s) AND active
                    LIMIT 1
                    """,
                    (company,),
                )
                row = cursor.fetchone()
        if row is None:
            raise ValueError(f"Company '{company}' was not found.")
        return int(row["id"])

    def delete(self, name: str) -> None:
        existing = self.get(name)
        if existing is None:
            raise KeyError(f"Supplier '{name}' was not found.")
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM suppliers WHERE lower(name) = lower(%s)",
                    (existing.name,),
                )
                if cursor.rowcount == 0:
                    raise KeyError(f"Supplier '{name}' was not found.")

    def bulk_import_master_data(
        self,
        *,
        suppliers: list[tuple[str, str]],
        approvals: list[
            tuple[str, str, str, str, str | None, str | None]
        ],
        terms: list[
            tuple[
                str,
                str,
                str | None,
                str | None,
                str | None,
                str | None,
            ]
        ],
    ) -> None:
        """Persist a parsed workbook in one transaction using pipelined batches."""
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO suppliers (
                        name, default_company_id, default_company
                    )
                    SELECT item.name, company.id, item.company
                    FROM jsonb_to_recordset(%s::jsonb)
                        AS item(name text, company text)
                    JOIN companies AS company
                      ON lower(company.name) = lower(item.company)
                     AND company.active
                    ON CONFLICT (lower(name)) DO UPDATE
                    SET default_company_id = excluded.default_company_id,
                        default_company = excluded.default_company,
                        updated_at = now()
                    """,
                    (json.dumps([
                        {"name": supplier, "company": company}
                        for supplier, company in suppliers
                    ]),),
                )
                if approvals:
                    cursor.execute(
                        """
                        INSERT INTO approval_matrix (
                            company, supplier, approver1_name, approver1_email,
                            approver2_name, approver2_email
                        )
                        SELECT company, supplier, approver1_name, approver1_email,
                               approver2_name, approver2_email
                        FROM jsonb_to_recordset(%s::jsonb) AS item(
                            company text,
                            supplier text,
                            approver1_name text,
                            approver1_email text,
                            approver2_name text,
                            approver2_email text
                        )
                        ON CONFLICT (lower(company), lower(supplier)) DO UPDATE
                        SET approver1_name = excluded.approver1_name,
                            approver1_email = excluded.approver1_email,
                            approver2_name = excluded.approver2_name,
                            approver2_email = excluded.approver2_email
                        """,
                        (json.dumps([
                            {
                                "company": row[0],
                                "supplier": row[1],
                                "approver1_name": row[2],
                                "approver1_email": row[3],
                                "approver2_name": row[4],
                                "approver2_email": row[5],
                            }
                            for row in approvals
                        ]),),
                    )
                account_terms = [row for row in terms if row[2] is not None]
                if account_terms:
                    cursor.execute(
                        """
                        INSERT INTO supplier_terms (
                            company, supplier, supplier_account_number,
                            default_payment_method, payment_terms_notice,
                            bank_account
                        )
                        SELECT company, supplier, supplier_account_number,
                               default_payment_method, payment_terms_notice,
                               bank_account
                        FROM jsonb_to_recordset(%s::jsonb) AS item(
                            company text,
                            supplier text,
                            supplier_account_number text,
                            default_payment_method text,
                            payment_terms_notice text,
                            bank_account text
                        )
                        ON CONFLICT (company, supplier_account_number) DO UPDATE
                        SET supplier = excluded.supplier,
                            default_payment_method = excluded.default_payment_method,
                            payment_terms_notice = excluded.payment_terms_notice,
                            bank_account = excluded.bank_account
                        """,
                        (json.dumps([
                            {
                                "company": row[0],
                                "supplier": row[1],
                                "supplier_account_number": row[2],
                                "default_payment_method": row[3],
                                "payment_terms_notice": row[4],
                                "bank_account": row[5],
                            }
                            for row in account_terms
                        ]),),
                    )
                default_terms = [row for row in terms if row[2] is None]
                if default_terms:
                    cursor.execute(
                        """
                        INSERT INTO supplier_terms (
                            company, supplier, supplier_account_number,
                            default_payment_method, payment_terms_notice,
                            bank_account
                        )
                        SELECT company, supplier, supplier_account_number,
                               default_payment_method, payment_terms_notice,
                               bank_account
                        FROM jsonb_to_recordset(%s::jsonb) AS item(
                            company text,
                            supplier text,
                            supplier_account_number text,
                            default_payment_method text,
                            payment_terms_notice text,
                            bank_account text
                        )
                        ON CONFLICT (company, supplier)
                            WHERE supplier_account_number IS NULL
                        DO UPDATE SET
                            default_payment_method = excluded.default_payment_method,
                            payment_terms_notice = excluded.payment_terms_notice,
                            bank_account = excluded.bank_account
                        """,
                        (json.dumps([
                            {
                                "company": row[0],
                                "supplier": row[1],
                                "supplier_account_number": row[2],
                                "default_payment_method": row[3],
                                "payment_terms_notice": row[4],
                                "bank_account": row[5],
                            }
                            for row in default_terms
                        ]),),
                    )

    def _ensure_identifiers_available(
        self,
        name: str,
        aliases: tuple[str, ...],
        *,
        excluding_name: str | None = None,
    ) -> None:
        if not name:
            raise ValueError("A supplier name is required.")
        excluded_key = excluding_name.strip().casefold() if excluding_name else None
        requested = {name.casefold(), *(alias.casefold() for alias in aliases)}
        for profile in self.list():
            if profile.name.strip().casefold() == excluded_key:
                continue
            existing = {
                profile.name.strip().casefold(),
                *(alias.strip().casefold() for alias in profile.aliases),
            }
            if requested & existing:
                raise ValueError(
                    "Supplier names and aliases must be unique regardless of "
                    "capitalisation."
                )


class PostgresApprovalMatrixStore(_PostgresStore):
    def list(self) -> list[ApprovalMatrixEntry]:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, company, supplier, approver1_name, approver1_email,
                           approver2_name, approver2_email
                    FROM approval_matrix
                    ORDER BY company, supplier
                    """
                )
                rows = cursor.fetchall()
        return [_approval(row) for row in rows]

    def find(self, company: str, supplier: str) -> ApprovalMatrixEntry | None:
        exact = self.find_exact(company, supplier)
        return exact or self.find_exact(ALL_COMPANIES, supplier)

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

    def create(self, **fields: object) -> ApprovalMatrixEntry:
        try:
            with self._connection_factory() as connection:  # type: ignore[attr-defined]
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO approval_matrix (
                            company, supplier, approver1_name, approver1_email,
                            approver2_name, approver2_email
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        tuple(
                            fields.get(key)
                            for key in (
                                "company",
                                "supplier",
                                "approver1_name",
                                "approver1_email",
                                "approver2_name",
                                "approver2_email",
                            )
                        ),
                    )
                    row = cursor.fetchone()
        except Exception as error:
            if _is_unique_violation(error):
                raise ValueError(
                    f"An approval matrix entry for '{fields.get('company')}' / "
                    f"'{fields.get('supplier')}' already exists."
                ) from error
            raise
        if row is None:
            raise RuntimeError("PostgreSQL did not return the approval matrix ID.")
        result = self._get_by_id(int(row["id"]))
        if result is None:
            raise RuntimeError("Approval matrix entry disappeared after creation.")
        return result

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
            result = self._get_by_id(entry_id)
            if result is None:
                raise KeyError(f"Approval matrix entry {entry_id} was not found.")
            return result
        assignments = ", ".join(f"{key} = %s" for key in fields)
        try:
            with self._connection_factory() as connection:  # type: ignore[attr-defined]
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE approval_matrix SET {assignments} WHERE id = %s",
                        (*fields.values(), entry_id),
                    )
                    if cursor.rowcount == 0:
                        raise KeyError(
                            f"Approval matrix entry {entry_id} was not found."
                        )
        except Exception as error:
            if _is_unique_violation(error):
                raise ValueError(
                    "An approval matrix entry for that company and supplier "
                    "already exists."
                ) from error
            raise
        result = self._get_by_id(entry_id)
        if result is None:
            raise RuntimeError(
                f"Approval matrix entry {entry_id} disappeared after it was updated."
            )
        return result

    def delete(self, entry_id: int) -> None:
        self._delete("id = %s", (entry_id,), f"Approval matrix entry {entry_id}")

    def delete_by_supplier(self, supplier: str) -> int:
        return self._delete_many("lower(supplier) = lower(%s)", (supplier,))

    def delete_by_company(self, company: str) -> int:
        return self._delete_many("lower(company) = lower(%s)", (company,))

    def _get_by_id(self, entry_id: int) -> ApprovalMatrixEntry | None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, company, supplier, approver1_name, approver1_email,
                           approver2_name, approver2_email
                    FROM approval_matrix WHERE id = %s
                    """,
                    (entry_id,),
                )
                row = cursor.fetchone()
        return _approval(row) if row else None

    def _delete(self, where: str, values: tuple[object, ...], label: str) -> None:
        count = self._delete_many(where, values)
        if count == 0:
            raise KeyError(f"{label} was not found.")

    def _delete_many(self, where: str, values: tuple[object, ...]) -> int:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM approval_matrix WHERE {where}", values)
                return cursor.rowcount


class PostgresSupplierTermsStore(_PostgresStore):
    def list(self) -> list[SupplierTerms]:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, company, supplier, supplier_account_number,
                           default_payment_method, payment_terms_notice, bank_account
                    FROM supplier_terms ORDER BY company, supplier
                    """
                )
                rows = cursor.fetchall()
        return [_terms(row) for row in rows]

    def get(self, company: str, supplier: str) -> SupplierTerms | None:
        values = self.list_for_supplier(company, supplier)
        return values[0] if values else None

    def list_for_supplier(self, company: str, supplier: str) -> list[SupplierTerms]:
        company_key = company.strip().casefold()
        supplier_key = supplier.strip().casefold()
        matches = [
            terms
            for terms in self.list()
            if terms.supplier.strip().casefold() == supplier_key
            and terms.company.strip().casefold() in (company_key, ALL_COMPANIES)
        ]
        specific = [
            terms for terms in matches
            if terms.company.strip().casefold() == company_key
        ]
        return specific or [terms for terms in matches if terms.company == ALL_COMPANIES]

    def upsert(self, **fields: object) -> SupplierTerms:
        company = str(fields["company"])
        supplier = str(fields["supplier"])
        account = fields.get("supplier_account_number")
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id FROM supplier_terms
                    WHERE lower(company) = lower(%s) AND (
                        supplier_account_number = %s
                        OR (supplier_account_number IS NULL AND %s::text IS NULL
                            AND lower(supplier) = lower(%s))
                    )
                    """,
                    (company, account, account, supplier),
                )
                existing = cursor.fetchone()
                values = (
                    supplier,
                    fields.get("default_payment_method"),
                    fields.get("payment_terms_notice"),
                    fields.get("bank_account"),
                )
                if existing:
                    terms_id = int(existing["id"])
                    cursor.execute(
                        """
                        UPDATE supplier_terms
                        SET supplier = %s, default_payment_method = %s,
                            payment_terms_notice = %s, bank_account = %s
                        WHERE id = %s
                        """,
                        (*values, terms_id),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO supplier_terms (
                            company, supplier, supplier_account_number,
                            default_payment_method, payment_terms_notice, bank_account
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (company, supplier, account, *values[1:]),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("PostgreSQL did not return supplier terms ID.")
                    terms_id = int(row["id"])
        result = self._get_by_id(terms_id)
        if result is None:
            raise RuntimeError("Supplier terms disappeared after upsert.")
        return result

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
            result = self._get_by_id(terms_id)
            if result is None:
                raise KeyError(f"Supplier terms entry {terms_id} was not found.")
            return result
        assignments = ", ".join(f"{key} = %s" for key in fields)
        try:
            with self._connection_factory() as connection:  # type: ignore[attr-defined]
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE supplier_terms SET {assignments} WHERE id = %s",
                        (*fields.values(), terms_id),
                    )
                    if cursor.rowcount == 0:
                        raise KeyError(
                            f"Supplier terms entry {terms_id} was not found."
                        )
        except Exception as error:
            if _is_unique_violation(error):
                raise ValueError(
                    "A payment profile with that company and account number "
                    "already exists."
                ) from error
            raise
        result = self._get_by_id(terms_id)
        if result is None:
            raise RuntimeError(
                f"Supplier terms entry {terms_id} disappeared after it was updated."
            )
        return result

    def delete(self, company: str, supplier: str) -> None:
        count = self._delete_many(
            "lower(company) = lower(%s) AND lower(supplier) = lower(%s)",
            (company, supplier),
        )
        if count == 0:
            raise KeyError(f"No supplier terms found for '{company}' / '{supplier}'.")

    def delete_by_supplier(self, supplier: str) -> int:
        return self._delete_many("lower(supplier) = lower(%s)", (supplier,))

    def delete_by_company(self, company: str) -> int:
        return self._delete_many("lower(company) = lower(%s)", (company,))

    def _get_by_id(self, terms_id: int) -> SupplierTerms | None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, company, supplier, supplier_account_number,
                           default_payment_method, payment_terms_notice, bank_account
                    FROM supplier_terms WHERE id = %s
                    """,
                    (terms_id,),
                )
                row = cursor.fetchone()
        return _terms(row) if row else None

    def _delete_many(self, where: str, values: tuple[object, ...]) -> int:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM supplier_terms WHERE {where}", values)
                return cursor.rowcount


class PostgresProcessConfigurationStore(_PostgresStore):
    def get(self, key: str, default: str | None = None) -> str | None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT value FROM process_configuration WHERE key = %s",
                    (key,),
                )
                row = cursor.fetchone()
        return str(row["value"]) if row else default

    def set(self, key: str, value: str) -> None:
        with self._connection_factory() as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO process_configuration (key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE
                    SET value = excluded.value, updated_at = now()
                    """,
                    (key, value),
                )


def postgres_config_stores(
    settings: PostgresSettings | None = None,
) -> tuple[
    PostgresCompanyStore,
    PostgresSupplierStore,
    PostgresApprovalMatrixStore,
    PostgresSupplierTermsStore,
    PostgresProcessConfigurationStore,
]:
    factory = postgres_connection_factory(settings)
    return (
        PostgresCompanyStore(connection_factory=factory),
        PostgresSupplierStore(connection_factory=factory),
        PostgresApprovalMatrixStore(connection_factory=factory),
        PostgresSupplierTermsStore(connection_factory=factory),
        PostgresProcessConfigurationStore(connection_factory=factory),
    )


def _match_named(
    profiles: Iterable[CompanyProfile] | Iterable[SupplierProfile], name: str
) -> CompanyProfile | SupplierProfile | None:
    key = name.strip().casefold()
    for profile in profiles:
        if profile.name.strip().casefold() == key:
            return profile
        if any(alias.strip().casefold() == key for alias in profile.aliases):
            return profile
    return None


def _company(row: dict[str, object]) -> CompanyProfile:
    return CompanyProfile(
        name=str(row["name"]),
        sharepoint_root_folder=str(row["sharepoint_root_folder"] or ""),
        company_folder=str(row["company_folder"]),
        po_matching_folder=str(row["po_matching_folder"]),
        aliases=_aliases(row["aliases"]),
        vat_number=_optional(row["vat_number"]),
        address=_optional(row["address"]),
    )


def _supplier(row: dict[str, object]) -> SupplierProfile:
    return SupplierProfile(
        name=str(row["name"]),
        aliases=_aliases(row["aliases"]),
        default_company=_optional(row["default_company"]),
        contact_email=_optional(row["contact_email"]),
        invoice_number_pattern=_optional(row["invoice_number_pattern"]),
    )


def _approval(row: dict[str, object]) -> ApprovalMatrixEntry:
    approver2 = (
        Approver(
            name=str(row["approver2_name"]),
            email=str(row["approver2_email"] or ""),
        )
        if row["approver2_name"]
        else None
    )
    return ApprovalMatrixEntry(
        id=int(row["id"]),
        company=str(row["company"]),
        supplier=str(row["supplier"]),
        approver1=Approver(
            name=str(row["approver1_name"]),
            email=str(row["approver1_email"]),
        ),
        approver2=approver2,
    )


def _terms(row: dict[str, object]) -> SupplierTerms:
    return SupplierTerms(
        id=int(row["id"]),
        company=str(row["company"]),
        supplier=str(row["supplier"]),
        supplier_account_number=_optional(row["supplier_account_number"]),
        default_payment_method=_optional(row["default_payment_method"]),
        payment_terms_notice=_optional(row["payment_terms_notice"]),
        bank_account=_optional(row["bank_account"]),
    )


def _aliases(value: object) -> tuple[str, ...]:
    return tuple(alias for alias in str(value or "").split(",") if alias)


def _optional(value: object) -> str | None:
    return str(value) if value is not None else None


def _is_unique_violation(error: Exception) -> bool:
    return getattr(error, "sqlstate", None) == "23505"
