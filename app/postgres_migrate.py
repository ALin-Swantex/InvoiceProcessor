from __future__ import annotations

from app.environment import load_project_environment
from app.postgres_invoices import PostgresInvoiceStore
from app.postgres_settings import PostgresSettings


def main() -> None:
    load_project_environment()
    PostgresInvoiceStore(
        PostgresSettings.from_env(),
        initialize_schema=True,
    )
    print("PostgreSQL migrations applied successfully.")


if __name__ == "__main__":
    main()
