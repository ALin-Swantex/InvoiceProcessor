from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.config_db import connect
from app.invoice_number_validation import validate_invoice_number_pattern


# ---------------------------------------------------------------------------
# Suppliers — admin-maintained master data.
#
# SOFTWARE_SPEC.md section 7 requires the approval matrix (and by extension
# the supplier list backing it) to be "easy for an authorised member of
# staff to maintain". This mirrors app/companies.py: a SQLite-backed store
# today, intended to become a SharePoint "Suppliers" List in production.
#
# Matching a name against this list happens on canonical name or alias,
# case-insensitively -- never a fuzzy/guessed match. If the AI-extracted
# supplier does not match anything here, the invoice must be flagged for
# manual review rather than silently proceeding (see MANUAL_VS_AUTOMATED.md).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupplierProfile:
    name: str
    aliases: tuple[str, ...] = ()
    default_company: str | None = None
    contact_email: str | None = None
    invoice_number_pattern: str | None = None


_DEFAULT_SUPPLIERS: list[SupplierProfile] = [
    SupplierProfile(name="Supplier Ltd", aliases=("Supplier Limited",)),
    SupplierProfile(name="Northgate Supplies", aliases=("Northgate",)),
]


class SupplierStore:
    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path
        with self._connect() as connection:
            existing = connection.execute("SELECT COUNT(*) AS n FROM suppliers").fetchone()
            if not existing["n"]:
                for profile in _DEFAULT_SUPPLIERS:
                    self._insert(connection, profile)
                connection.commit()

    def list(self) -> list[SupplierProfile]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
        return [_row_to_profile(row) for row in rows]

    def get(self, name: str) -> SupplierProfile | None:
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
        with self._connect() as connection:
            try:
                self._insert(connection, profile)
                connection.commit()
            except sqlite3.IntegrityError as error:
                raise ValueError(f"Supplier '{name}' already exists.") from error
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
                existing.name,
                aliases,
                excluding_name=existing.name,
            )
            fields["aliases"] = ",".join(aliases)
        if "invoice_number_pattern" in fields:
            fields["invoice_number_pattern"] = validate_invoice_number_pattern(
                fields["invoice_number_pattern"]
                if isinstance(fields["invoice_number_pattern"], str)
                else None
            )
        if not fields:
            return existing
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE suppliers SET {assignments} WHERE name = ?",
                (*fields.values(), existing.name),
            )
            connection.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"Supplier '{name}' was not found.")
        updated = self.get(existing.name)
        if updated is None:
            raise RuntimeError(f"Supplier '{name}' disappeared after it was updated.")
        return updated

    def delete(self, name: str) -> None:
        existing = self.get(name)
        if existing is None:
            raise KeyError(f"Supplier '{name}' was not found.")
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM suppliers WHERE name = ?",
                (existing.name,),
            )
            connection.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"Supplier '{name}' was not found.")

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

    def _insert(self, connection: sqlite3.Connection, profile: SupplierProfile) -> None:
        connection.execute(
            """
            INSERT INTO suppliers (
                name, aliases, default_company, contact_email,
                invoice_number_pattern
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                profile.name,
                ",".join(profile.aliases),
                profile.default_company,
                profile.contact_email,
                profile.invoice_number_pattern,
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect(self.database_path)


def _row_to_profile(row: sqlite3.Row) -> SupplierProfile:
    aliases = tuple(a for a in (row["aliases"] or "").split(",") if a)
    return SupplierProfile(
        name=row["name"],
        aliases=aliases,
        default_company=row["default_company"],
        contact_email=row["contact_email"],
        invoice_number_pattern=row["invoice_number_pattern"],
    )


def _normalize_aliases(aliases: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        value = str(alias).strip()
        key = value.casefold()
        if value and key not in seen:
            normalized.append(value)
            seen.add(key)
    return tuple(normalized)


_default_store: SupplierStore | None = None


def _store() -> SupplierStore:
    global _default_store
    if _default_store is None:
        _default_store = SupplierStore()
    return _default_store


def list_suppliers() -> list[SupplierProfile]:
    return _store().list()


def get_supplier(name: str) -> SupplierProfile | None:
    return _store().get(name)
