from __future__ import annotations

import os
import sqlite3
from pathlib import Path


# ---------------------------------------------------------------------------
# Shared SQLite store for admin-maintained business configuration:
# Companies, Suppliers, and the Approval Matrix.
#
# The specification requires these to be "easy for an authorised member of
# staff to maintain... without having to change the underlying [workflow]
# code" (SOFTWARE_SPEC.md section 7). Storing them in SQLite behind a CRUD
# API (see main.py's /api/admin/* endpoints, restricted to the admin role)
# satisfies that requirement for the prototype.
#
# In a production Microsoft 365 deployment this module would instead read
# and write a SharePoint List (e.g. "Companies", "Suppliers",
# "Approval Matrix") through Microsoft Graph, exactly as GENERAL_PROCESS.md
# describes. The table shapes below intentionally mirror those list designs
# so that swap is mostly a storage-layer change.
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    name TEXT PRIMARY KEY,
    company_folder TEXT NOT NULL,
    po_matching_folder TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    vat_number TEXT,
    address TEXT
);

CREATE TABLE IF NOT EXISTS suppliers (
    name TEXT PRIMARY KEY,
    aliases TEXT NOT NULL DEFAULT '',
    default_company TEXT,
    contact_email TEXT
);

CREATE TABLE IF NOT EXISTS approval_matrix (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    supplier TEXT NOT NULL,
    approver1_name TEXT NOT NULL,
    approver1_email TEXT NOT NULL,
    approver2_name TEXT,
    approver2_email TEXT,
    UNIQUE(company, supplier)
);

CREATE TABLE IF NOT EXISTS supplier_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    supplier TEXT NOT NULL,
    supplier_account_number TEXT,
    default_payment_method TEXT,
    payment_terms_notice TEXT,
    bank_account TEXT,
    UNIQUE(company, supplier_account_number)
);

CREATE TABLE IF NOT EXISTS process_configuration (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def config_database_path() -> Path:
    return Path(os.environ.get("CONFIG_DB_PATH", "runtime_data/config.db"))


def connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = database_path or config_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


def get_setting(key: str, default: str | None = None, *, database_path: Path | None = None) -> str | None:
    """Read an admin-tunable setting (e.g. the AI confidence threshold) from
    the process_configuration table. Falls back to `default` if unset."""
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT value FROM process_configuration WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row is not None else default


def set_setting(key: str, value: str, *, database_path: Path | None = None) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO process_configuration (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        connection.commit()
