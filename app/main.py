from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

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
from app.deps import company_response as _company_response  # noqa: F401
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
from app.schemas import (  # noqa: F401 -- re-exported for existing importers
    ROLES_APPROVERS_ADMIN,
    ROLES_PURCHASE_LEDGER_ADMIN,
    ApprovalDecisionRequest,
    ApprovalMatrixRequest,
    ApprovalMatrixUpdateRequest,
    CompanyRequest,
    CompanyUpdateRequest,
    ConfirmationRequest,
    FlagReviewRequest,
    ForeignAllocationRequest,
    InvoiceConfirmRequest,
    IrjConfigurationUpdateRequest,
    LoginRequest,
    PaymentRequest,
    PaymentRouteRequest,
    PoMatchRequest,
    ReconciliationRequest,
    RejectInvoiceRequest,
    ResumeApprovalRequest,
    ReviewDecisionRequest,
    SageRegistrationRequest,
    StatementFileRequest,
    SupplierRequest,
    SupplierTermsUpdateRequest,
    SupplierUpdateRequest,
    ThresholdUpdateRequest,
)
from app.sharepoint import SharePointClient
from app.suppliers import SupplierStore
from app.supplier_terms import SupplierTermsStore


STATIC_DIR = Path(__file__).resolve().parent / "static"


def _index_html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


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
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
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
    app.state.sharepoint_folder_paths = None
    app.state.sharepoint_company_structures = None
    app.state.auth_store = auth_store or auth_store_from_environment()
    app.state.entra_auth_client = (
        entra_auth_client
        if entra_auth_client is not None
        else entra_auth_client_from_environment()
    )
    app.state.local_login_enabled = os.environ.get(
        "AUTH_LOCAL_LOGIN_ENABLED", "false"
    ).strip().casefold() in {"1", "true", "yes", "on"}
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
            "OUTLOOK_MCP_TENANT_ID",
            "OUTLOOK_MCP_CLIENT_ID",
            "OUTLOOK_MCP_CLIENT_SECRET",
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
