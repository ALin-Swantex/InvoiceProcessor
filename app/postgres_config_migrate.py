from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from app.environment import load_project_environment
from app.postgres_settings import PostgresSettings, postgres_connection_factory


TABLE_COLUMNS = {
    "companies": (
        "name", "sharepoint_root_folder", "company_folder",
        "po_matching_folder", "aliases", "vat_number", "address",
    ),
    "suppliers": (
        "name", "aliases", "default_company", "contact_email",
        "invoice_number_pattern",
    ),
    "approval_matrix": (
        "company", "supplier", "approver1_name", "approver1_email",
        "approver2_name", "approver2_email",
    ),
    "supplier_terms": (
        "company", "supplier", "supplier_account_number",
        "default_payment_method", "payment_terms_notice", "bank_account",
    ),
    "process_configuration": ("key", "value"),
}


def migrate_sqlite_configuration(
    source: Path,
    *,
    connection_factory=None,
) -> dict[str, int]:
    if not source.is_file():
        raise FileNotFoundError(f"SQLite configuration database not found: {source}")
    factory = connection_factory or postgres_connection_factory(
        PostgresSettings.from_env()
    )
    source_connection = sqlite3.connect(source)
    source_connection.row_factory = sqlite3.Row
    try:
        source_rows = {
            table: source_connection.execute(
                f"SELECT {', '.join(columns)} FROM {table}"
            ).fetchall()
            for table, columns in TABLE_COLUMNS.items()
        }
    finally:
        source_connection.close()

    with factory() as connection:
        with connection.cursor() as cursor:
            for row in source_rows["companies"]:
                cursor.execute(
                    """
                    INSERT INTO companies (
                        name, sharepoint_root_folder, company_folder,
                        po_matching_folder, aliases, vat_number, address
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (lower(name)) DO UPDATE SET
                        sharepoint_root_folder = excluded.sharepoint_root_folder,
                        company_folder = excluded.company_folder,
                        po_matching_folder = excluded.po_matching_folder,
                        aliases = excluded.aliases,
                        vat_number = excluded.vat_number,
                        address = excluded.address,
                        active = true,
                        updated_at = now()
                    """,
                    tuple(row),
                )
            for row in source_rows["suppliers"]:
                cursor.execute(
                    """
                    INSERT INTO suppliers (
                        name, aliases, default_company, contact_email,
                        invoice_number_pattern
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (lower(name)) DO UPDATE SET
                        aliases = excluded.aliases,
                        default_company = excluded.default_company,
                        contact_email = excluded.contact_email,
                        invoice_number_pattern = excluded.invoice_number_pattern,
                        active = true,
                        updated_at = now()
                    """,
                    tuple(row),
                )
            for row in source_rows["approval_matrix"]:
                cursor.execute(
                    """
                    INSERT INTO approval_matrix (
                        company, supplier, approver1_name, approver1_email,
                        approver2_name, approver2_email
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (lower(company), lower(supplier)) DO UPDATE SET
                        approver1_name = excluded.approver1_name,
                        approver1_email = excluded.approver1_email,
                        approver2_name = excluded.approver2_name,
                        approver2_email = excluded.approver2_email
                    """,
                    tuple(row),
                )
            for row in source_rows["supplier_terms"]:
                values = tuple(row)
                account = row["supplier_account_number"]
                if account is None:
                    conflict = (
                        "(company, supplier) "
                        "WHERE supplier_account_number IS NULL"
                    )
                else:
                    conflict = "(company, supplier_account_number)"
                cursor.execute(
                    f"""
                    INSERT INTO supplier_terms (
                        company, supplier, supplier_account_number,
                        default_payment_method, payment_terms_notice, bank_account
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT {conflict} DO UPDATE SET
                        supplier = excluded.supplier,
                        default_payment_method = excluded.default_payment_method,
                        payment_terms_notice = excluded.payment_terms_notice,
                        bank_account = excluded.bank_account
                    """,
                    values,
                )
            for row in source_rows["process_configuration"]:
                cursor.execute(
                    """
                    INSERT INTO process_configuration (key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET
                        value = excluded.value, updated_at = now()
                    """,
                    tuple(row),
                )
    return {table: len(rows) for table, rows in source_rows.items()}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy SQLite admin configuration into PostgreSQL."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("runtime_data/config.db"),
        help="Path to the existing SQLite configuration database.",
    )
    args = parser.parse_args()
    load_project_environment()
    counts = migrate_sqlite_configuration(args.source)
    print(
        "PostgreSQL configuration imported: "
        + ", ".join(f"{table}={count}" for table, count in counts.items())
    )


if __name__ == "__main__":
    main()
