from __future__ import annotations

import logging
import math
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse, Response
from starlette.types import Scope

from app.activity_feed import ActivityFeedStore
from app.ai_extraction import ai_extraction_configured  # noqa: F401 -- patched by tests; resolved at call time by app.routers.invoices
from app.approval_matrix import ApprovalMatrixStore
from app.auth import AuthStore, auth_store_from_environment
from app.entra_auth import (
    EntraAuthClient,
    entra_auth_client_from_environment,
)
from app.companies import CompanyStore
from app.config_db import SQLiteProcessConfigurationStore
from app.environment import load_project_environment, project_path_from_environment
from app.invoice_lifecycle import InvoiceLifecycle
from app.invoices import InvoiceStore
from app.irj import IrjNumberGenerator
from app.outlook_notifications import OutlookNotificationStore
from app.routers import (
    activity as activity_routes,
    admin as admin_routes,
    auth as auth_routes,
    invoices as invoice_routes,
    outlook as outlook_routes,
    reference as reference_routes,
    statements as statement_routes,
    ui as ui_routes,
)
from app.sharepoint import SharePointClient
from app.suppliers import SupplierStore
from app.supplier_terms import SupplierTermsStore


STATIC_DIR = Path(__file__).resolve().parent / "static"


_INDEX_HTML_CACHE: dict[str, object] = {"mtime_ns": None, "text": ""}


def _index_html() -> str:
    """Return index.html, re-reading it only when the file changes.

    Keyed on the file's mtime so a deploy (or local edit) is picked up
    without restarting, while normal page loads avoid disk reads.
    """
    path = STATIC_DIR / "index.html"
    mtime_ns = path.stat().st_mtime_ns
    if _INDEX_HTML_CACHE["mtime_ns"] != mtime_ns:
        _INDEX_HTML_CACHE["text"] = path.read_text(encoding="utf-8")
        _INDEX_HTML_CACHE["mtime_ns"] = mtime_ns
    return str(_INDEX_HTML_CACHE["text"])


class RevalidatingStaticFiles(StaticFiles):
    """Static files that browsers may cache but must revalidate.

    ``no-cache`` means "store, but check with the server before use", so
    the ETag/Last-Modified conditional request turns repeat loads into
    cheap 304s while a deploy is still picked up immediately.
    """

    def file_response(
        self,
        full_path: "os.PathLike[str] | str",
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(
            full_path, stat_result, scope, status_code
        )
        response.headers["Cache-Control"] = "no-cache"
        return response


logger = logging.getLogger("invoice_processor")
logger.setLevel(logging.INFO)
if not logger.handlers:
    # Uvicorn does not configure the root logger, so without our own handler
    # these INFO-level startup/mode messages would be silently dropped.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))
    logger.addHandler(_handler)


def _build_invoice_store_from_environment(invoice_db_path: Path) -> InvoiceStore:
    """Build the application's authoritative PostgreSQL invoice store."""
    del invoice_db_path
    from app.postgres_invoices import create_postgres_invoice_store

    return create_postgres_invoice_store()  # type: ignore[return-value]


DATABASE_UNAVAILABLE_MESSAGE = (
    "The database is unreachable. Check the network or VPN connection and the "
    "AZURE_POSTGRES_* settings, then try again."
)


def _register_database_error_handlers(app: FastAPI) -> None:
    """Report an unreachable PostgreSQL as a clear 503, not a hung request.

    Without this, any route that touches the database (for example the Microsoft
    sign-in, which saves its OAuth state) waits for the connection pool timeout
    and then fails with an unexplained server error.
    """
    try:
        from psycopg import OperationalError
        from psycopg_pool import PoolTimeout
    except ImportError:  # SQLite-only installs have no psycopg.
        return

    async def database_unavailable(request: object, error: Exception) -> JSONResponse:
        logger.error("Database unavailable: %s: %s", type(error).__name__, error)
        return JSONResponse({"detail": DATABASE_UNAVAILABLE_MESSAGE}, status_code=503)

    app.add_exception_handler(PoolTimeout, database_unavailable)
    app.add_exception_handler(OperationalError, database_unavailable)


async def _validation_error(request: object, error: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422, except that a rejected infinity or NaN is echoed
    back as text: as a number it cannot be written as JSON, and the error
    response itself would fail with a 500."""

    def printable(value: object) -> object:
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value

    errors = [
        {**item, "input": printable(item["input"])} if "input" in item else item
        for item in error.errors()
    ]
    return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)


def create_app(
    *,
    notification_store: OutlookNotificationStore | None = None,
    invoice_store: InvoiceStore | None = None,
    webhook_client_state: str | None = None,
    sharepoint_client: SharePointClient | None = None,
    irj_generator: IrjNumberGenerator | None = None,
    activity_feed: ActivityFeedStore | None = None,
    auth_store: AuthStore | None = None,
    companies_store: CompanyStore | None = None,
    suppliers_store: SupplierStore | None = None,
    approval_matrix_store: ApprovalMatrixStore | None = None,
    supplier_terms_store: SupplierTermsStore | None = None,
    process_configuration_store: SQLiteProcessConfigurationStore | None = None,
    entra_auth_client: EntraAuthClient | None = None,
    auto_configure_sharepoint: bool = True,
) -> FastAPI:
    app = FastAPI(title="Invoice Intake Prototype", version="0.1.0")
    # GZipMiddleware skips text/event-stream by default, so the SSE
    # activity stream is not buffered by compression.
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.mount(
        "/static", RevalidatingStaticFiles(directory=STATIC_DIR), name="static"
    )
    _register_database_error_handlers(app)
    app.add_exception_handler(RequestValidationError, _validation_error)
    if notification_store is None:
        from app.postgres_notifications import PostgresOutlookNotificationStore
        from app.postgres_monitoring import PostgresWorkerMonitor

        app.state.notification_store = PostgresOutlookNotificationStore()
        app.state.worker_monitor = PostgresWorkerMonitor()
    else:
        app.state.notification_store = notification_store
        app.state.worker_monitor = None
    invoice_db_path = project_path_from_environment(
        "INVOICE_DB_PATH", "runtime_data/invoices.db"
    )
    invoice_store_backend = "postgres" if invoice_store is None else "injected"
    app.state.invoice_store = invoice_store or _build_invoice_store_from_environment(
        invoice_db_path
    )
    app.state.webhook_client_state = (
        webhook_client_state
        if webhook_client_state is not None
        else os.environ.get("OUTLOOK_WEBHOOK_CLIENT_STATE", "")
    )
    app.state.sharepoint_client = sharepoint_client
    # The IRJ generator must share a database with whichever invoice_store is
    # actually in use (see app/irj.py docstring) so IRJ numbering and invoice
    # records stay in the same file. Previously this always fell back to
    # invoice_db_path (the env-derived default), completely ignoring a custom
    # invoice_store passed in by a caller -- e.g. every pytest run injected an
    # isolated tmp_path InvoiceStore but the IRJ generator (and, via the same
    # bug, the activity feed below) still silently pointed at the real
    # runtime_data/invoices.db and runtime_data/activity_feed.db. That meant
    # every test run permanently bumped the *production* IRJ sequence and
    # wrote fabricated "approval_pending" / "approved" / "paid" / "reconciled"
    # activity events into the *live* Activity Feed the user actually
    # watches in the app -- making genuinely still-pending invoices look like
    # they had skipped approvers and gone straight to Approved.
    irj_db_path = getattr(app.state.invoice_store, "database_path", invoice_db_path)
    if irj_generator is not None:
        app.state.irj_generator = irj_generator
    elif invoice_store is None:
        from app.postgres_services import PostgresIrjNumberGenerator

        app.state.irj_generator = PostgresIrjNumberGenerator()
    else:
        app.state.irj_generator = IrjNumberGenerator(irj_db_path)
    if activity_feed is not None:
        app.state.activity_feed = activity_feed
    elif invoice_store is None:
        from app.postgres_services import PostgresActivityFeedStore

        app.state.activity_feed = PostgresActivityFeedStore()
    else:
        app.state.activity_feed = ActivityFeedStore(
            project_path_from_environment(
                "ACTIVITY_FEED_DB_PATH", "runtime_data/activity_feed.db"
            )
        )
    app.state.auth_store = auth_store or auth_store_from_environment()
    app.state.entra_auth_client = (
        entra_auth_client
        if entra_auth_client is not None
        else entra_auth_client_from_environment()
    )
    app.state.local_login_enabled = os.environ.get(
        "AUTH_LOCAL_LOGIN_ENABLED", "false"
    ).strip().casefold() in {"1", "true", "yes", "on"}
    if app.state.local_login_enabled and app.state.entra_auth_client:
        logger.warning(
            "AUTH_LOCAL_LOGIN_ENABLED is on while Microsoft Entra sign-in is "
            "configured. The local seed passwords are public in the repository: "
            "local login must never be enabled on a reachable deployment."
        )
    use_postgres_config = (
        invoice_store is None
        and companies_store is None
        and suppliers_store is None
        and approval_matrix_store is None
        and supplier_terms_store is None
    )
    if use_postgres_config:
        from app.postgres_config import postgres_config_stores

        (
            app.state.companies_store,
            app.state.suppliers_store,
            app.state.approval_matrix_store,
            app.state.supplier_terms_store,
            app.state.process_configuration_store,
        ) = postgres_config_stores()
    else:
        config_path = project_path_from_environment(
            "CONFIG_DB_PATH", "runtime_data/config.db"
        )
        app.state.companies_store = companies_store or CompanyStore(config_path)
        app.state.suppliers_store = suppliers_store or SupplierStore(config_path)
        app.state.approval_matrix_store = (
            approval_matrix_store or ApprovalMatrixStore(config_path)
        )
        app.state.supplier_terms_store = (
            supplier_terms_store or SupplierTermsStore(config_path)
        )
        app.state.process_configuration_store = (
            process_configuration_store
            or SQLiteProcessConfigurationStore(config_path)
        )
    app.state.lifecycle = InvoiceLifecycle(
        app.state.invoice_store,
        app.state.irj_generator,
        app.state.activity_feed,
        app.state.sharepoint_client,
        companies_store=app.state.companies_store,
        approval_matrix_store=app.state.approval_matrix_store,
        suppliers_store=app.state.suppliers_store,
        supplier_terms_store=app.state.supplier_terms_store,
        configuration_getter=app.state.process_configuration_store.get,
    )
    app.state.sharepoint_attach_attempted = (
        app.state.sharepoint_client is not None or not auto_configure_sharepoint
    )
    app.state.sharepoint_company_sync_complete = False

    def _sharepoint_startup_status() -> str:
        # The previous message only reported whether a client had been injected
        # directly, so a fully configured deployment still logged "not yet
        # configured" simply because the client is attached lazily on first use.
        if app.state.sharepoint_client is not None:
            return "configured"
        if not auto_configure_sharepoint:
            return "disabled"
        required = (
            "SHAREPOINT_SITE_ID",
            "SHAREPOINT_DRIVE_ID",
            "GRAPH_TENANT_ID",
            "GRAPH_CLIENT_ID",
            "GRAPH_CLIENT_SECRET",
        )
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            return f"not configured (missing {', '.join(missing)})"
        return "configured from environment (client attaches on first use)"

    logger.info(
        "Invoice Processor starting: invoice store backend=%s, SharePoint filing=%s",
        invoice_store_backend,
        _sharepoint_startup_status(),
    )

    # Include order follows the original single-module registration order.
    # (The statements and /api/workflow/confirm routes moved relative to the
    # invoice routes, but no paths overlap, so matching is unchanged.)
    app.include_router(ui_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(reference_routes.router)
    app.include_router(activity_routes.router)
    app.include_router(invoice_routes.router)
    app.include_router(statement_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(outlook_routes.router)

    return app


def __getattr__(name: str) -> object:
    """Build the default application lazily.

    ``uvicorn app.main:app`` resolves ``app`` through this hook, so importing
    this module (for example from tests) no longer loads ``.env`` or connects
    store configuration as a side effect.
    """
    if name == "app":
        load_project_environment()
        default_app = create_app()
        globals()["app"] = default_app
        return default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
