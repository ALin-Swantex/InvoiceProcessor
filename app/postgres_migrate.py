from __future__ import annotations

from app.environment import load_project_environment
from app.postgres_invoices import PostgresInvoiceStore
from app.postgres_settings import PostgresSettings, close_postgres_pools


def main() -> None:
    load_project_environment()
    try:
        PostgresInvoiceStore(
            PostgresSettings.from_env(),
            initialize_schema=True,
        )
        print("PostgreSQL migrations applied successfully.")
    finally:
        # Close the pool so the command exits immediately.
        close_postgres_pools()


if __name__ == "__main__":
    main()
