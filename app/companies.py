from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.company_folders import CompanyFolderStructure
from app.config_db import connect


# ---------------------------------------------------------------------------
# Companies — admin-maintained master data (see app/config_db.py docstring).
#
# Company matching is deliberately conservative: a supplied name matches a
# company only if it equals the company's canonical name or one of its
# configured aliases (case-insensitively). Anything else is left unmatched
# so Purchase Ledger is asked to confirm rather than the system guessing —
# see SOFTWARE_SPEC.md (the application must not guess).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompanyProfile:
    name: str
    sharepoint_root_folder: str
    company_folder: str
    po_matching_folder: str
    aliases: tuple[str, ...] = ()
    vat_number: str | None = None
    address: str | None = None


_DEFAULT_COMPANIES: list[CompanyProfile] = [
    CompanyProfile(
        name="Acme Trading Ltd",
        sharepoint_root_folder="Invoices/Acme Trading Ltd",
        company_folder="Invoices/Acme Trading Ltd/Nominal Invoices",
        po_matching_folder="Invoices/Acme Trading Ltd/PO Invoices/PO Match",
        aliases=("Acme Trading", "Acme"),
    ),
    CompanyProfile(
        name="Northfield Manufacturing",
        sharepoint_root_folder="Invoices/Northfield Manufacturing",
        company_folder="Invoices/Northfield Manufacturing/Nominal Invoices",
        po_matching_folder="Invoices/Northfield Manufacturing/PO Invoices/PO Match",
        aliases=("Northfield Mfg",),
    ),
    CompanyProfile(
        name="Riverside Logistics",
        sharepoint_root_folder="Invoices/Riverside Logistics",
        company_folder="Invoices/Riverside Logistics/Nominal Invoices",
        po_matching_folder="Invoices/Riverside Logistics/PO Invoices/PO Match",
        aliases=(),
    ),
]


class CompanyStore:
    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path
        with self._connect() as connection:
            existing = connection.execute("SELECT COUNT(*) AS n FROM companies").fetchone()
            if not existing["n"]:
                for profile in _DEFAULT_COMPANIES:
                    self._insert(connection, profile)
                connection.commit()

    def list(self) -> list[CompanyProfile]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM companies ORDER BY name"
            ).fetchall()
        return [_row_to_profile(row) for row in rows]

    def get(self, name: str) -> CompanyProfile | None:
        """Match on canonical name or any configured alias, case-insensitively."""
        key = name.strip().casefold()
        for profile in self.list():
            if profile.name.strip().casefold() == key:
                return profile
            if any(alias.strip().casefold() == key for alias in profile.aliases):
                return profile
        return None

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
        with self._connect() as connection:
            try:
                self._insert(connection, profile)
                connection.commit()
            except sqlite3.IntegrityError as error:
                raise ValueError(f"Company '{name}' already exists.") from error
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
        if "aliases" in fields and isinstance(fields["aliases"], (list, tuple)):
            fields["aliases"] = ",".join(fields["aliases"])
        if not fields:
            existing = self.get(name)
            if existing is None:
                raise KeyError(f"Company '{name}' was not found.")
            return existing
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE companies SET {assignments} WHERE name = ?",
                (*fields.values(), name),
            )
            connection.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"Company '{name}' was not found.")
        updated = self.get(name)
        if updated is None:
            raise RuntimeError(f"Company '{name}' disappeared after it was updated.")
        return updated

    def delete(self, name: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM companies WHERE name = ?", (name,))
            connection.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"Company '{name}' was not found.")

    def _insert(self, connection: sqlite3.Connection, profile: CompanyProfile) -> None:
        connection.execute(
            """
            INSERT INTO companies (
                name, sharepoint_root_folder, company_folder,
                po_matching_folder, aliases, vat_number, address
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
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

    def _connect(self) -> sqlite3.Connection:
        return connect(self.database_path)


def _row_to_profile(row: sqlite3.Row) -> CompanyProfile:
    aliases = tuple(a for a in (row["aliases"] or "").split(",") if a)
    return CompanyProfile(
        name=row["name"],
        sharepoint_root_folder=row["sharepoint_root_folder"],
        company_folder=row["company_folder"],
        po_matching_folder=row["po_matching_folder"],
        aliases=aliases,
        vat_number=row["vat_number"],
        address=row["address"],
    )


# ---------------------------------------------------------------------------
# Module-level convenience wrapper (default store) so existing call sites
# such as `from app.companies import list_companies` keep working without
# every caller needing to construct a CompanyStore explicitly.
# ---------------------------------------------------------------------------

_default_store: CompanyStore | None = None


def _store() -> CompanyStore:
    global _default_store
    if _default_store is None:
        _default_store = CompanyStore()
    return _default_store


def list_companies() -> list[CompanyProfile]:
    return _store().list()


def get_company(name: str) -> CompanyProfile | None:
    return _store().get(name)
