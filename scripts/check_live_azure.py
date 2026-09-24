"""Read-only smoke checks for the configured Azure services.

Run from the repository root with ``python -m scripts.check_live_azure``.
No mailbox messages, files, invoices, or credentials are printed.
"""

from __future__ import annotations

import os
import socket
from urllib.parse import quote

import httpx

from app.environment import load_project_environment


class SchemaCheckError(RuntimeError):
    """A safe, locally constructed schema diagnostic."""


def _check_postgres() -> str:
    from app.invoices import InvoiceRecord
    from app.postgres_settings import PostgresSettings, connect_postgres

    settings = PostgresSettings.from_env()
    with socket.create_connection((settings.host, settings.port), timeout=5):
        pass
    with connect_postgres(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            cursor.fetchone()
            cursor.execute("SELECT to_regclass('public.invoices') IS NOT NULL AS exists")
            row = cursor.fetchone()
            if not row["exists"]:
                raise SchemaCheckError("invoices table missing; apply migrations")
            cursor.execute("SELECT count(*) AS count FROM public.invoices")
            count = cursor.fetchone()["count"]
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'invoices'"
            )
            columns = {row["column_name"] for row in cursor.fetchall()}
            missing = set(InvoiceRecord.__dataclass_fields__) - columns
            if missing:
                raise SchemaCheckError(
                    f"{len(missing)} invoice metadata column(s) missing; "
                    "apply migrations"
                )
            cursor.execute(
                "SELECT to_regclass('public.bi_invoice_metadata') IS NOT NULL "
                "AS invoice_view, "
                "to_regclass('public.bi_invoice_events') IS NOT NULL AS events_view"
            )
            views = cursor.fetchone()
    if not views["invoice_view"] or not views["events_view"]:
        raise SchemaCheckError("reporting views missing; apply migrations")
    return f"connected; {count} invoice row(s); all metadata columns and reporting views present"


def _graph_token() -> str:
    from app.outlook_graph import MsalTokenProvider, settings_from_environment

    return MsalTokenProvider(settings_from_environment())()


def _check_outlook(token: str) -> str:
    mailbox = os.environ["OUTLOOK_MCP_MAILBOX"]
    url = (
        "https://graph.microsoft.com/v1.0/users/"
        f"{quote(mailbox, safe='')}/mailFolders/inbox"
    )
    response = httpx.get(
        url,
        params={"$select": "id"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    response.raise_for_status()
    return "Inbox readable"


def _check_sharepoint(token: str) -> str:
    drive = os.environ["SHAREPOINT_DRIVE_ID"]
    url = f"https://graph.microsoft.com/v1.0/drives/{quote(drive, safe='')}/root"
    response = httpx.get(
        url,
        params={"$select": "id"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    response.raise_for_status()
    return "document-library root readable"


def _check_document_intelligence() -> str:
    from azure.ai.documentintelligence import DocumentIntelligenceAdministrationClient
    from azure.identity import ClientSecretCredential

    credential = ClientSecretCredential(
        os.environ["OUTLOOK_MCP_TENANT_ID"],
        os.environ["OUTLOOK_MCP_CLIENT_ID"],
        os.environ["OUTLOOK_MCP_CLIENT_SECRET"],
    )
    with credential:
        with DocumentIntelligenceAdministrationClient(
            endpoint=os.environ["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"],
            credential=credential,
        ) as client:
            client.get_resource_details()
    return "resource readable (invoice analysis requires a sample PDF)"


def main() -> int:
    load_project_environment()
    checks = [("PostgreSQL", _check_postgres)]
    token: str | None = None
    try:
        token = _graph_token()
        print("Graph token: acquired")
    except Exception as error:
        print(f"Graph token: FAILED ({type(error).__name__})")
    if token:
        checks.extend(
            [
                ("Outlook", lambda: _check_outlook(token)),
                ("SharePoint", lambda: _check_sharepoint(token)),
            ]
        )
    checks.append(("Document Intelligence", _check_document_intelligence))
    failed = token is None
    for name, check in checks:
        try:
            print(f"{name}: {check()}")
        except httpx.HTTPStatusError as error:
            failed = True
            print(f"{name}: FAILED (HTTP {error.response.status_code})")
        except SchemaCheckError as error:
            failed = True
            print(f"{name}: FAILED ({error})")
        except Exception as error:
            failed = True
            print(f"{name}: FAILED ({type(error).__name__})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
