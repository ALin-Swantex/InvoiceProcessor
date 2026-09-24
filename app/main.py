from __future__ import annotations

import asyncio
import io
import logging
import os
import secrets
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response as FastAPIResponse, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel

from app.activity_feed import ActivityFeedStore, ROLE_PURCHASE_LEDGER
from app.ai_extraction import ai_extraction_configured
from app.approval_matrix import ALL_COMPANIES, ApprovalMatrixStore
from app.auth import (
    AuthError,
    AuthStore,
    ROLE_ADMIN,
    ROLE_APPROVER_1,
    ROLE_APPROVER_2,
    SESSION_COOKIE_NAME,
    User,
    auth_store_from_environment,
    get_current_user,
    require_role,
)
from app.bulk_import import find_existing_supplier_imports, import_supplier_workbook
from app.entra_auth import (
    EntraAuthClient,
    entra_auth_client_from_environment,
)
from app.company_folders import (
    FLAGGED_INVOICES_FOLDER,
    INCOMING_INVOICES_FOLDER,
    REJECTED_INVOICES_FOLDER,
    CompanyFolderStructure,
    discover_company_folder_structures,
)
from app.companies import CompanyProfile, CompanyStore
from app.config_db import (
    SQLiteProcessConfigurationStore,
    configuration_backend,
)
from app.environment import load_project_environment, project_path_from_environment
from app.invoice_lifecycle import (
    InvoiceExtractionUnavailableError,
    InvoiceLifecycle,
    InvoiceLifecycleError,
)
from app.pdf_validation import InvalidPdfError, validate_pdf
from app.sharepoint_intake import SharePointIncomingMonitor
from app.invoices import InvoiceStore
from app.irj import IrjNumberGenerator
from app.metrics import build_metrics
from app.outlook_notifications import (
    OutlookNotificationStore,
    extract_message_id,
)
from app.sharepoint import SharePointClient, SharePointError
from app.suppliers import SupplierStore
from app.supplier_terms import SupplierTermsStore
from app.workflow import (
    ConfirmedInvoice,
    RoutingValidationError,
    route_confirmed_invoice,
)

load_project_environment()

logger = logging.getLogger("invoice_processor")
logger.setLevel(logging.INFO)
if not logger.handlers:
    # Uvicorn does not configure the root logger, so without our own handler
    # these INFO-level startup/mode messages would be silently dropped.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))
    logger.addHandler(_handler)


# ---------------------------------------------------------------------------
# Role access map — mirrors MANUAL_VS_AUTOMATED.md. Every state-changing
# endpoint and every admin configuration endpoint is restricted to the
# role(s) that should be able to perform that action; read endpoints are
# open to any signed-in user so every role can see the full pipeline (the
# frontend nav then only surfaces the sections/actions relevant to the
# signed-in role).
# ---------------------------------------------------------------------------
ROLES_PURCHASE_LEDGER_ADMIN = (ROLE_PURCHASE_LEDGER, ROLE_ADMIN)
ROLES_APPROVERS_ADMIN = (ROLE_APPROVER_1, ROLE_APPROVER_2, ROLE_ADMIN)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ConfirmationRequest(BaseModel):
    """Raw routing calculator request. Kept for direct testing of
    app.workflow.route_confirmed_invoice without any persistence or
    SharePoint side effects."""

    invoice_id: str
    company: str
    company_folder: str
    original_filename: str
    irj_number: str
    purchase_order_number: str | None = None
    po_matching_folder: str | None = None
    purchase_ledger_recipient: str | None = None


class InvoiceConfirmRequest(BaseModel):
    """Purchase Ledger's confirmation of AI-extracted (or manually entered)
    invoice fields. Triggers IRJ numbering, PO/nominal routing, SharePoint
    filing, and approver assignment."""

    company: str
    supplier: str
    supplier_invoice_number: str | None = None
    purchase_order_number: str | None = None
    invoice_date: str | None = None
    invoice_value: float | None = None
    currency: str | None = "GBP"
    override_duplicate: bool = False
    correction_reason: str | None = None


class ApprovalDecisionRequest(BaseModel):
    level: int
    decision: str
    comments: str | None = None


class PoMatchRequest(BaseModel):
    matched: bool
    notes: str | None = None
    query_category: str | None = None
    purchasing_contact: str | None = None


class FlagReviewRequest(BaseModel):
    reason: str


class ReviewDecisionRequest(BaseModel):
    accepted: bool
    reason: str | None = None


class StatementFileRequest(BaseModel):
    company: str


class PaymentRequest(BaseModel):
    payment_date: str
    supplier_account_number: str | None = None
    payment_reference: str
    payment_method: str | None = None


class ReconciliationRequest(BaseModel):
    reconciliation_date: str
    notes: str | None = None


class ResumeApprovalRequest(BaseModel):
    resolution_notes: str | None = None


class SageRegistrationRequest(BaseModel):
    irj_number: str


class PaymentRouteRequest(BaseModel):
    route: str


class RejectInvoiceRequest(BaseModel):
    reason: str


class ForeignAllocationRequest(BaseModel):
    allocation_date: str
    allocation_reference: str


class LoginRequest(BaseModel):
    username: str
    password: str


class CompanyRequest(BaseModel):
    name: str
    sharepoint_root_folder: str | None = None
    company_folder: str | None = None
    po_matching_folder: str | None = None
    aliases: list[str] | None = None
    vat_number: str | None = None
    address: str | None = None


class CompanyUpdateRequest(BaseModel):
    sharepoint_root_folder: str | None = None
    company_folder: str | None = None
    po_matching_folder: str | None = None
    aliases: list[str] | None = None
    vat_number: str | None = None
    address: str | None = None


class SupplierRequest(BaseModel):
    name: str
    aliases: list[str] | None = None
    default_company: str | None = None
    contact_email: str | None = None
    invoice_number_pattern: str | None = None


class SupplierUpdateRequest(BaseModel):
    aliases: list[str] | None = None
    default_company: str | None = None
    contact_email: str | None = None
    invoice_number_pattern: str | None = None


class ApprovalMatrixRequest(BaseModel):
    company: str
    supplier: str
    approver1_name: str
    approver1_email: str
    approver2_name: str | None = None
    approver2_email: str | None = None


class ApprovalMatrixUpdateRequest(BaseModel):
    company: str | None = None
    supplier: str | None = None
    approver1_name: str | None = None
    approver1_email: str | None = None
    approver2_name: str | None = None
    approver2_email: str | None = None


class SupplierTermsUpdateRequest(BaseModel):
    company: str
    supplier: str
    supplier_account_number: str | None = None
    default_payment_method: str | None = None
    payment_terms_notice: str | None = None
    bank_account: str | None = None


class ThresholdUpdateRequest(BaseModel):
    threshold: float


class IrjConfigurationUpdateRequest(BaseModel):
    mode: str
    current_irj: str | None = None


def _build_invoice_store_from_environment(invoice_db_path: Path) -> InvoiceStore:
    """Build the application's authoritative PostgreSQL invoice store."""
    del invoice_db_path
    from app.postgres_invoices import create_postgres_invoice_store

    return create_postgres_invoice_store()  # type: ignore[return-value]


def _company_response(profile: CompanyProfile) -> dict[str, object]:
    structure = CompanyFolderStructure.from_root(profile.sharepoint_root_folder)
    return {
        **asdict(profile),
        "folder_structure": structure.as_dict(),
    }


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

    def _lifecycle() -> InvoiceLifecycle:
        # Lazily attach a SharePoint client from the environment the first
        # time it is needed, so tests that never touch SharePoint never pay
        # for constructing one, but real deployments pick up .env values
        # without an explicit override. Only attempt this once per process
        # -- if SharePoint isn't configured, retrying on every request would
        # just repeat the same failure silently.
        current: InvoiceLifecycle = app.state.lifecycle
        if (
            current.sharepoint_client is None
            and app.state.sharepoint_client is None
            and not app.state.sharepoint_attach_attempted
        ):
            app.state.sharepoint_attach_attempted = True
            try:
                from app.sharepoint import sharepoint_client_from_environment

                client = sharepoint_client_from_environment()
                app.state.sharepoint_client = client
                current.sharepoint_client = client
                logger.info("SharePoint client configured; invoice PDFs will be filed centrally.")
            except Exception as error:
                logger.info(
                    "SharePoint filing disabled (%s: %s). Running in local test mode -- "
                    "invoices will complete their workflow locally without being filed "
                    "in SharePoint.",
                    type(error).__name__,
                    error,
                )
        return current

    def _sync_sharepoint_companies() -> list[CompanyProfile]:
        """Import complete SharePoint company folders into the company store."""
        if app.state.sharepoint_company_sync_complete:
            return []
        client = _lifecycle().sharepoint_client
        if client is None:
            return []
        try:
            structures = discover_company_folder_structures(
                client.list_folder_paths()
            )
        except SharePointError as error:
            logger.warning("SharePoint company synchronization failed: %s", error)
            return []

        existing = app.state.companies_store.list()
        existing_names = {profile.name.strip().casefold() for profile in existing}
        existing_roots = {
            profile.sharepoint_root_folder.strip().casefold()
            for profile in existing
        }
        created: list[CompanyProfile] = []
        for structure in structures:
            name = structure.root.rsplit("/", 1)[-1]
            if (
                name.casefold() in existing_names
                or structure.root.casefold() in existing_roots
            ):
                continue
            try:
                profile = app.state.companies_store.create(
                    name=name,
                    sharepoint_root_folder=structure.root,
                )
            except ValueError:
                # Another worker may have inserted the same company.
                continue
            created.append(profile)
            existing_names.add(profile.name.casefold())
            existing_roots.add(profile.sharepoint_root_folder.casefold())
        app.state.sharepoint_company_sync_complete = True
        if created:
            logger.info(
                "Imported %d company folder(s) from SharePoint into configuration.",
                len(created),
            )
        return created

    @app.get("/", response_class=HTMLResponse)
    def prototype_home() -> str:
        return """
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>Swantex | Invoice Processing</title>
          <style>
            :root {
              color-scheme: light;
              font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
              color: #182230;
              background: #f4f7fb;
            }
            * { box-sizing: border-box; }
            body { margin: 0; min-width: 320px; }
            #app-root {
              min-height: 100vh; display: grid;
              grid-template-columns: 240px minmax(0, 1fr);
            }
            .sidebar {
              position: sticky; top: 0; height: 100vh; display: flex;
              flex-direction: column; padding: 1.25rem .9rem; color: white;
              background: #102a43; border-right: 4px solid #2f80ed;
              overflow-y: auto;
            }
            .brand {
              padding: .25rem .65rem 1.2rem; margin-bottom: .65rem;
              border-bottom: 1px solid rgba(255,255,255,.14);
            }
            .brand h1 { margin: 0; font-size: 1.55rem; letter-spacing: -.02em; }
            .brand p {
              margin: .25rem 0 0; color: #9fb3c8; font-size: .76rem;
              font-weight: 600; text-transform: uppercase; letter-spacing: .08em;
            }
            .header-controls {
              display: flex; flex-direction: column; align-items: stretch;
              gap: .65rem; margin-top: auto; padding: 1rem .4rem 0;
              border-top: 1px solid rgba(255,255,255,.14);
            }
            .role-switcher-label {
              display: flex; align-items: center; gap: .4rem;
              padding: .3rem .6rem .3rem .7rem; border-radius: 999px;
              border: 1px dashed #f6ad55; background: #3a2a12;
              color: #fbd38d; font-size: .78rem; font-weight: 600;
            }
            .role-switcher {
              padding: .4rem .6rem; border-radius: 7px; border: 1px solid #325377;
              background: #1c3a5e; color: white; font: inherit; font-size: .8rem;
              cursor: pointer;
            }
            nav.section-nav { display: flex; flex-direction: column; gap: .3rem; }
            nav.section-nav button {
              display: flex; align-items: center; justify-content: space-between;
              width: 100%; border: 1px solid transparent; background: transparent;
              color: #bcccdc; border-radius: 8px; padding: .62rem .7rem;
              font-size: .8rem; font-weight: 700; text-align: left; cursor: pointer;
            }
            nav.section-nav button:hover { background: rgba(255,255,255,.08); color: white; }
            nav.section-nav button.active {
              background: #2f80ed; color: white; border-color: #5da0f3;
            }
            nav.section-nav button .count {
              display: inline-grid; place-items: center; min-width: 1.45rem;
              margin-left: .35rem; padding: .08rem .38rem; border-radius: 999px;
              background: rgba(255,255,255,.1); font-size: .7rem;
            }
            nav.section-nav button.active .count { background: rgba(255,255,255,.25); }
            main { width: 100%; max-width: 1500px; margin: 0 auto; padding: 1.25rem; }
            .layout {
              display: grid; grid-template-columns: minmax(360px, .9fr) minmax(520px, 1.35fr);
              gap: 1rem; align-items: start;
            }
            .card {
              background: white; border: 1px solid #d9e2ec; border-radius: 12px;
              box-shadow: 0 6px 20px rgba(16, 42, 67, .06); overflow: hidden;
            }
            .card-header {
              display: flex; justify-content: space-between; gap: 1rem; align-items: center;
              padding: .9rem 1rem; border-bottom: 1px solid #e4e7eb;
            }
            .card-header h2 { font-size: 1rem; margin: 0; }
            .badge {
              border-radius: 999px; background: #edf2f7; color: #627d98;
              padding: .3rem .55rem; font-size: .75rem; font-weight: 700;
            }
            .pdf-empty {
              min-height: 610px; display: grid; place-items: center; text-align: center;
              padding: 2rem; background:
                linear-gradient(135deg, rgba(240,244,248,.8), rgba(255,255,255,.9));
              color: #7b8794;
            }
            .pdf-frame { width: 100%; min-height: 610px; border: 0; display: none; }
            .pdf-icon {
              display: grid; place-items: center; width: 64px; height: 80px;
              margin: 0 auto 1rem; border: 2px solid #9fb3c8; border-radius: 6px;
              color: #486581; font-weight: 800;
            }
            .content { padding: 1rem; }
            .notice {
              display: grid; grid-template-columns: auto 1fr; gap: .7rem;
              padding: .8rem; margin-bottom: 1rem; border-radius: 8px;
              background: #fffbea; border: 1px solid #f7d070; color: #6b4f00;
            }
            .manual-upload {
              margin-bottom: 1.2rem; border: 1px dashed #9fb3c8; border-radius: 8px;
              padding: .7rem .9rem; background: #f8fafc;
            }
            .manual-upload summary {
              cursor: pointer; font-weight: 700; color: #2f80ed; font-size: .85rem;
            }
            .manual-upload-hint {
              margin: .5rem 0 .8rem; color: #52606d; font-size: .78rem;
            }
            #manual-upload-status {
              margin-top: .6rem; font-size: .78rem;
            }
            #manual-upload-status.success { color: #1a7f37; }
            #manual-upload-status.error { color: #c53030; }
            .section-title {
              margin: 1.2rem 0 .65rem; font-size: .82rem; color: #52606d;
              text-transform: uppercase; letter-spacing: .05em;
            }
            .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .75rem; }
            .grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
            label { display: grid; gap: .3rem; color: #52606d; font-size: .78rem; }
            input, select, textarea {
              width: 100%; min-height: 40px; border: 1px solid #cbd5e1;
              border-radius: 7px; background: #f8fafc; padding: .55rem .65rem;
              color: #7b8794; font: inherit;
            }
            input:not(:disabled), select:not(:disabled), textarea:not(:disabled) {
              background: white; color: #182230;
            }
            textarea { min-height: 72px; resize: vertical; }
            input:disabled, select:disabled, textarea:disabled { opacity: 1; cursor: not-allowed; }
            .confidence { display: flex; align-items: center; gap: .4rem; }
            .confidence input { flex: 1; }
            .confidence small { min-width: 48px; color: #829ab1; }
            .actions {
              display: flex; gap: .65rem; justify-content: flex-end; flex-wrap: wrap;
              padding: 1rem; border-top: 1px solid #e4e7eb; background: #f8fafc;
            }
            #delete-invoice-button { margin-right: auto; }
            button {
              border: 1px solid transparent; border-radius: 7px; padding: .65rem .9rem;
              font: inherit; font-weight: 700; cursor: pointer;
              transition: transform .08s ease, box-shadow .12s ease,
                background-color .12s ease, border-color .12s ease;
            }
            button:not(:disabled):hover {
              transform: translateY(-1px);
              box-shadow: 0 3px 8px rgba(16, 42, 67, .16);
            }
            button:not(:disabled):active {
              transform: translateY(1px);
              box-shadow: inset 0 2px 4px rgba(16, 42, 67, .2);
            }
            button:focus-visible, a.action-link:focus-visible {
              outline: 3px solid rgba(47, 128, 237, .35);
              outline-offset: 2px;
            }
            button[aria-busy="true"] { cursor: wait; }
            button:disabled { background: #d9e2ec; color: #829ab1; cursor: not-allowed; }
            .secondary { background: white; border: 1px solid #bcccdc; color: #486581; }
            .secondary:disabled { border: 1px solid #bcccdc; background: white; }
            button.primary { background: #2f80ed; color: white; }
            button.danger { background: #fee2e2; color: #9b1c1c; }
            .invoice-picker { margin-bottom: 1rem; }
            .tab-panel { display: none; }
            .tab-panel.active { display: block; }
            table.section-table { width: 100%; border-collapse: collapse; font-size: .85rem; }
            table.section-table th, table.section-table td {
              text-align: left; padding: .6rem .55rem; border-bottom: 1px solid #e4e7eb;
            }
            table.section-table tr.invoice-summary-row {
              cursor: pointer;
            }
            table.section-table tr.invoice-summary-row:hover,
            table.section-table tr.invoice-summary-row:focus {
              background: #f0f7ff;
              outline: none;
            }
            table.section-table tr.invoice-summary-row[aria-expanded="true"] {
              background: #e8f2ff;
            }
            .invoice-expand-hint {
              display: block; padding-top: .45rem; border-top: 1px solid #d9e2ec;
              color: #52606d; font-size: .7rem; font-weight: 700;
            }
            .invoice-analysis-cell {
              padding: 0 !important; background: #f8fbff;
            }
            .invoice-analysis {
              display: grid; grid-template-columns: minmax(320px, 1.2fr) minmax(280px, .8fr);
              gap: 1rem; padding: 1rem;
            }
            .invoice-analysis-pdf {
              width: 100%; min-height: 620px; border: 1px solid #bcccdc;
              border-radius: 8px; background: white;
            }
            .invoice-analysis-fields {
              min-width: 0; padding: .9rem; border: 1px solid #d9e2ec;
              border-radius: 8px; background: white;
            }
            .invoice-analysis-fields h3 {
              margin: 0 0 .75rem; color: #243b53; font-size: .95rem;
            }
            .invoice-analysis-fields h4 {
              margin: 1rem 0 .45rem; color: #486581; font-size: .75rem;
              letter-spacing: .035em; text-transform: uppercase;
            }
            .invoice-analysis-list {
              display: grid; grid-template-columns: minmax(120px, .7fr) minmax(0, 1.3fr);
              gap: .45rem .75rem; margin: 0;
            }
            .invoice-analysis-list dt {
              color: #627d98; font-size: .74rem; font-weight: 700;
            }
            .invoice-analysis-list dd {
              min-width: 0; margin: 0; color: #243b53; font-size: .8rem;
              overflow-wrap: anywhere;
            }
            .field-confidence {
              display: inline-block; margin-left: .35rem; padding: .1rem .35rem;
              border-radius: 999px; background: #e8f2ff; color: #1f5f99;
              font-size: .66rem; font-weight: 700;
            }
            .analysis-warning {
              margin-top: .75rem; padding: .65rem; border-left: 3px solid #d97706;
              background: #fff8e7; color: #7c4a03; font-size: .78rem;
              line-height: 1.45; white-space: pre-wrap;
            }
            .analysis-history {
              display: grid; gap: .55rem; margin-top: .4rem; padding: .75rem;
              border: 1px solid #d9e2ec; border-radius: 8px; background: #f8fafc;
            }
            .analysis-history-item {
              padding-bottom: .55rem; border-bottom: 1px solid #e4e7eb;
              color: #334e68; font-size: .78rem; line-height: 1.45;
              white-space: pre-wrap; overflow-wrap: anywhere;
            }
            .analysis-history-item:last-child { padding-bottom: 0; border-bottom: 0; }
            .analysis-history-item strong { display: block; color: #102a43; }
            .supplier-match-prompt { grid-column: 1 / -1; margin: 0; }
            .supplier-match-actions {
              display: flex; align-items: center; gap: .55rem; flex-wrap: wrap;
              margin-top: .55rem;
            }
            table.section-table th { color: #52606d; font-size: .72rem; text-transform: uppercase; }
            table.section-table tr:last-child td { border-bottom: 0; }
            .invoice-description-cell { min-width: 180px; max-width: 280px; }
            .mini-description {
              min-height: 34px; padding: .48rem .58rem; border: 1px solid #d9e2ec;
              border-radius: 7px; background: #f8fafc; color: #334e68;
              line-height: 1.35; white-space: pre-wrap; overflow-wrap: anywhere;
            }
            .row-actions {
              display: grid; gap: .45rem; min-width: 190px;
            }
            .row-action-buttons {
              display: flex; align-items: center; gap: .45rem; flex-wrap: wrap;
            }
            .row-action-buttons button, a.action-link {
              display: inline-flex; align-items: center; justify-content: center;
              min-height: 34px; padding: .42rem .68rem; border-radius: 7px;
              font-size: .76rem; font-weight: 700; line-height: 1.2;
              text-decoration: none; white-space: nowrap; text-align: center;
            }
            a.action-link {
              border: 1px solid #8aacc8; background: #eef6ff; color: #185b91;
              transition: transform .08s ease, box-shadow .12s ease,
                background-color .12s ease;
            }
            a.action-link:hover {
              background: #dceeff; transform: translateY(-1px);
              box-shadow: 0 3px 8px rgba(16, 42, 67, .14);
            }
            a.action-link:active {
              transform: translateY(1px);
              box-shadow: inset 0 2px 4px rgba(16, 42, 67, .18);
            }
            .row-action-buttons select {
              flex: 1 1 180px; min-width: 160px; min-height: 34px;
              padding: .4rem .55rem;
            }
            .tab-panel > .card > .content { overflow-x: auto; }
            .empty-state { padding: 2rem; text-align: center; color: #7b8794; }
            #toast-container {
              position: fixed; top: 1rem; right: 1rem; z-index: 1000;
              display: flex; flex-direction: column; gap: .5rem; max-width: 340px;
            }
            .toast {
              background: #102a43; color: white; border-radius: 8px; padding: .75rem .9rem;
              font-size: .82rem; box-shadow: 0 8px 24px rgba(16,42,67,.25);
              animation: toast-in .15s ease-out;
            }
            .toast.error { background: #9b1c1c; }
            dialog {
              width: min(460px, calc(100vw - 2rem)); border: 0; border-radius: 12px;
              padding: 0; box-shadow: 0 20px 60px rgba(16,42,67,.3);
            }
            dialog.admin-edit-dialog { width: min(640px, calc(100vw - 2rem)); }
            dialog::backdrop { background: rgba(11,31,51,.55); }
            .dialog-content { padding: 1rem; }
            .dialog-content h2 { margin: 0 0 .35rem; font-size: 1.05rem; }
            .dialog-content p { margin: 0 0 1rem; color: #52606d; font-size: .82rem; }
            .dialog-content .actions { margin: 1rem -1rem -1rem; }
            #sage-registration-error { color: #c53030; font-size: .8rem; min-height: 1em; margin-top: -.5rem; margin-bottom: .5rem; }
            .admin-edit-fields {
              display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
              gap: .75rem; margin-top: 1rem;
            }
            .admin-edit-fields label.full-width { grid-column: 1 / -1; }
            @media (max-width: 600px) {
              .admin-edit-fields { grid-template-columns: 1fr; }
              .admin-edit-fields label.full-width { grid-column: auto; }
            }
            @keyframes toast-in { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: translateY(0); } }
            @media (max-width: 950px) {
              #app-root { grid-template-columns: 190px minmax(0, 1fr); }
              .layout { grid-template-columns: 1fr; }
              .pdf-empty { min-height: 360px; }
            }
            @media (max-width: 700px) {
              #app-root { display: block; }
              .sidebar {
                position: static; width: 100%; height: auto; padding: .8rem;
                border-right: 0; border-bottom: 4px solid #2f80ed;
              }
              .brand { padding: 0 .2rem .7rem; margin-bottom: .65rem; }
              .brand h1 { font-size: 1.25rem; }
              nav.section-nav {
                flex-direction: row; overflow-x: auto; padding-bottom: .3rem;
              }
              nav.section-nav button {
                width: auto; flex: 0 0 auto; gap: .5rem; white-space: nowrap;
              }
              .header-controls {
                flex-direction: row; align-items: center; margin-top: .5rem;
                padding: .7rem .2rem 0;
              }
              main { padding: .8rem; }
              .grid, .grid.three { grid-template-columns: 1fr; }
            }
            #login-screen {
              position: fixed; inset: 0; z-index: 2000; display: grid;
              place-items: center; padding: 1.25rem;
              background:
                radial-gradient(circle at 18% 15%, rgba(47, 128, 237, .24), transparent 34%),
                radial-gradient(circle at 82% 85%, rgba(52, 211, 153, .12), transparent 30%),
                linear-gradient(145deg, #071727 0%, #102a43 55%, #173f68 100%);
            }
            #login-screen.hidden { display: none; }
            .login-card {
              position: relative; width: min(430px, 94vw); overflow: hidden;
              background: rgba(255, 255, 255, .98); border: 1px solid rgba(255,255,255,.7);
              border-radius: 18px; padding: 2.25rem;
              box-shadow: 0 28px 80px rgba(0,0,0,.38);
            }
            .login-card::before {
              content: ""; position: absolute; inset: 0 0 auto; height: 5px;
              background: linear-gradient(90deg, #2f80ed, #56ccf2);
            }
            .login-brand {
              display: flex; align-items: center; gap: .8rem; margin-bottom: 1.8rem;
            }
            .login-brand-mark {
              display: grid; place-items: center; width: 46px; height: 46px;
              border-radius: 12px; background: #102a43; color: white;
              font-size: 1.25rem; font-weight: 800; box-shadow: 0 8px 18px rgba(16,42,67,.2);
            }
            .login-brand strong { display: block; color: #102a43; font-size: 1.05rem; }
            .login-brand span {
              display: block; margin-top: .12rem; color: #829ab1;
              font-size: .72rem; font-weight: 700; letter-spacing: .08em;
              text-transform: uppercase;
            }
            .login-card h1 {
              margin: 0 0 .5rem; color: #102a43; font-size: 1.65rem;
              letter-spacing: -.025em;
            }
            .login-card p { margin: 0 0 1.35rem; color: #52606d; font-size: .88rem; line-height: 1.55; }
            .login-card label { margin-bottom: .7rem; }
            .microsoft-login {
              display: flex; align-items: center; justify-content: center; gap: .75rem;
              width: 100%; min-height: 48px; padding: .75rem 1rem;
              border: 1px solid #185abd; border-radius: 9px; background: #2f80ed;
              color: white; text-decoration: none; font-weight: 750;
              box-shadow: 0 7px 16px rgba(47,128,237,.22);
              transition: transform .08s ease, box-shadow .12s ease, background-color .12s ease;
            }
            .microsoft-login:hover {
              transform: translateY(-1px); background: #1f6fc9;
              box-shadow: 0 9px 20px rgba(47,128,237,.3);
            }
            .microsoft-mark {
              display: grid; grid-template-columns: repeat(2, 8px);
              grid-template-rows: repeat(2, 8px); gap: 2px;
            }
            .microsoft-mark span:nth-child(1) { background: #f35325; }
            .microsoft-mark span:nth-child(2) { background: #81bc06; }
            .microsoft-mark span:nth-child(3) { background: #05a6f0; }
            .microsoft-mark span:nth-child(4) { background: #ffba08; }
            .login-security {
              display: flex; align-items: center; justify-content: center; gap: .35rem;
              margin: 1rem 0 0; color: #829ab1; font-size: .72rem;
            }
            .login-security::before { content: "●"; color: #22a06b; font-size: .65rem; }
            .login-divider {
              display: flex; align-items: center; gap: .7rem; margin: 1.2rem 0;
              color: #9fb3c8; font-size: .72rem; text-transform: uppercase;
              letter-spacing: .06em;
            }
            .login-divider::before, .login-divider::after {
              content: ""; height: 1px; flex: 1; background: #d9e2ec;
            }
            #login-error { color: #c53030; font-size: .8rem; min-height: 1.1em; margin-bottom: .5rem; }
            #app-root.hidden { display: none; }
            .user-chip {
              display: flex; align-items: center; justify-content: space-between; gap: .5rem;
              padding: .45rem .55rem; border-radius: 8px; border: 1px solid #325377;
              background: #1c3a5e; color: #bee3f8; font-size: .75rem; font-weight: 600;
            }
            .user-chip button {
              background: transparent; border: 1px solid rgba(255,255,255,.4); color: white;
              padding: .25rem .55rem; font-size: .72rem; border-radius: 999px;
            }
            #admin-panel { display: grid; gap: 1rem; }
            .admin-block {
              min-width: 0; margin: 0; padding: 1rem;
              border: 1px solid #d9e2ec; border-radius: 10px; background: #fbfdff;
            }
            .admin-block h3 {
              margin: 0 0 .45rem; color: #243b53; font-size: .98rem;
            }
            .metrics-toolbar {
              display: flex; align-items: end; gap: .75rem; margin: .75rem 0;
            }
            .metrics-toolbar label { max-width: 220px; }
            .metrics-grid {
              display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
              gap: .8rem; margin-top: .8rem;
            }
            .metric-panel {
              min-width: 0; padding: .85rem; border: 1px solid #d9e2ec;
              border-radius: 8px; background: white;
            }
            .metric-panel.wide { grid-column: 1 / -1; }
            .metric-panel h4 { margin: 0 0 .65rem; color: #243b53; }
            .metric-bars { display: grid; gap: .35rem; }
            .metric-bar-row {
              display: grid; grid-template-columns: minmax(85px, 1fr) 3fr 35px;
              align-items: center; gap: .5rem; font-size: .78rem;
            }
            .metric-bar-track {
              height: 9px; overflow: hidden; border-radius: 999px; background: #e8eef5;
            }
            .metric-bar-fill { height: 100%; border-radius: inherit; background: #2878bd; }
            .metric-summary { font-size: 1.35rem; font-weight: 700; color: #102a43; }
            details.admin-collapsible { padding: 0; }
            details.admin-collapsible > summary {
              display: flex; align-items: center; justify-content: space-between;
              gap: 1rem; padding: 1rem; cursor: pointer; color: #243b53;
              font-size: .98rem; font-weight: 700; list-style: none;
              user-select: none;
            }
            details.admin-collapsible > summary::-webkit-details-marker {
              display: none;
            }
            details.admin-collapsible > summary::after {
              content: "⌄"; color: #52606d; font-size: 1.2rem;
              line-height: 1; transition: transform .15s ease;
            }
            details.admin-collapsible[open] > summary {
              border-bottom: 1px solid #d9e2ec;
            }
            details.admin-collapsible[open] > summary::after {
              transform: rotate(180deg);
            }
            .admin-collapsible-content { padding: 1rem; }
            .admin-company-groups {
              display: grid; gap: .65rem; margin-top: .85rem;
            }
            details.admin-company-group {
              overflow: hidden; border: 1px solid #d9e2ec;
              border-radius: 10px; background: white;
            }
            details.admin-company-group > summary {
              display: flex; align-items: center; justify-content: space-between;
              gap: .75rem; padding: .8rem 1rem; cursor: pointer;
              color: #243b53; background: #f8fafc; font-weight: 700;
              list-style: none; user-select: none;
            }
            details.admin-company-group > summary::-webkit-details-marker {
              display: none;
            }
            details.admin-company-group > summary::after {
              content: "⌄"; color: #52606d; transition: transform .15s ease;
            }
            details.admin-company-group[open] > summary::after {
              transform: rotate(180deg);
            }
            .admin-company-group-content { padding: .75rem; }
            .admin-help {
              max-width: 82ch; margin: 0 0 .9rem; color: #52606d;
              font-size: .82rem; line-height: 1.5;
            }
            .admin-form {
              display: grid;
              grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr));
              gap: .7rem; margin: .85rem 0 1rem; align-items: end;
            }
            .admin-form input, .admin-form select {
              width: 100%; min-width: 0; min-height: 40px;
            }
            .admin-form button {
              min-height: 40px; justify-self: start; white-space: nowrap;
            }
            #admin-company-root-folder { grid-column: span 2; }
            .admin-table-wrap {
              width: 100%; max-width: 100%; overflow-x: auto;
              border: 1px solid #d9e2ec; border-radius: 8px; background: white;
            }
            table.admin-table {
              width: 100%; border-collapse: collapse; font-size: .8rem;
            }
            table.admin-table th, table.admin-table td {
              padding: .65rem .7rem; border-bottom: 1px solid #e4e7eb;
              text-align: left; vertical-align: top; overflow-wrap: anywhere;
            }
            table.admin-table th {
              background: #f0f4f8; color: #52606d; font-size: .68rem;
              font-weight: 800; letter-spacing: .035em; text-transform: uppercase;
              white-space: normal;
            }
            table.admin-table tr:last-child td { border-bottom: 0; }
            table.admin-table td:last-child { width: 1%; white-space: nowrap; }
            table.admin-table button { padding: .4rem .6rem; font-size: .75rem; }
            .path-list {
              display: grid; gap: .45rem; min-width: min(320px, 50vw);
            }
            .path-list div { display: grid; gap: .08rem; }
            .path-list strong {
              color: #52606d; font-size: .66rem; letter-spacing: .035em;
              text-transform: uppercase;
            }
            .path-value {
              color: #243b53; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
              font-size: .73rem; line-height: 1.35; overflow-wrap: anywhere;
            }
            #admin-company-folder-preview {
              margin: .8rem 0 1rem; padding: .8rem; overflow-x: auto;
              border: 1px solid #d9e2ec; border-radius: 8px; background: white;
              color: #52606d; font-size: .8rem;
            }
            #admin-company-folder-preview table {
              width: 100%; border-collapse: collapse;
            }
            #admin-company-folder-preview th,
            #admin-company-folder-preview td {
              padding: .4rem .5rem; border-bottom: 1px solid #e4e7eb;
              text-align: left; vertical-align: top;
            }
            #admin-company-folder-preview th {
              width: 150px; color: #52606d; font-size: .68rem;
              text-transform: uppercase;
            }
            #admin-company-folder-preview td {
              font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
              overflow-wrap: anywhere;
            }
            @media (max-width: 700px) {
              .admin-block { padding: .8rem; }
              .admin-form { grid-template-columns: 1fr; }
              #admin-company-root-folder { grid-column: auto; }
              .admin-form button { width: 100%; }
              .path-list { min-width: 230px; }
              .invoice-analysis { grid-template-columns: 1fr; padding: .7rem; }
              .invoice-analysis-pdf { min-height: 480px; }
            }
          </style>
        </head>
        <body>
          <div id="toast-container"></div>

          <div id="login-screen">
            <form class="login-card" id="login-form">
              <div class="login-brand">
                <div class="login-brand-mark" aria-hidden="true">S</div>
                <div>
                  <strong>Swantex</strong>
                  <span>Invoice Processing</span>
                </div>
              </div>
              <h1>Welcome back</h1>
              <p>
                Sign in with your Swantex Microsoft 365 account to securely
                access invoice processing and approvals.
              </p>
              <div id="login-error"></div>
              <a
                id="microsoft-login-button"
                class="microsoft-login hidden"
                href="/api/auth/microsoft/login"
              >
                <span class="microsoft-mark" aria-hidden="true">
                  <span></span><span></span><span></span><span></span>
                </span>
                Continue with Microsoft 365
              </a>
              <div class="login-security">Protected by Microsoft Entra ID</div>
              <div id="local-login-fields">
                <div class="login-divider">Development access</div>
                <label>Username<input id="login-username" autocomplete="username"></label>
                <label>Password<input id="login-password" type="password" autocomplete="current-password"></label>
                <button type="submit" class="primary" style="width: 100%">Local sign in</button>
              </div>
            </form>
          </div>

          <dialog id="payment-dialog">
            <form id="payment-form" class="dialog-content">
              <h2>Record domestic payment</h2>
              <p id="payment-supplier-context"></p>
              <div class="grid">
                <label>Payment date
                  <input id="payment-date" type="date" required>
                </label>
                <label>Payment method
                  <input id="payment-method" disabled>
                </label>
                <label>Supplier account
                  <select id="payment-supplier-account"></select>
                </label>
                <label style="grid-column: 1 / -1">Payment reference
                  <input id="payment-reference" required>
                </label>
                <label>Normal payment terms
                  <input id="payment-terms" disabled>
                </label>
                <label>Pay from bank account
                  <input id="payment-bank-account" disabled>
                </label>
              </div>
              <div class="actions">
                <button type="button" class="secondary" id="payment-cancel">Cancel</button>
                <button type="submit" class="primary">Record payment</button>
              </div>
            </form>
          </dialog>

          <dialog id="sage-registration-dialog">
            <form id="sage-registration-form" class="dialog-content">
              <h2>Confirm Sage registration</h2>
              <p id="sage-registration-context"></p>
              <div class="grid">
                <label style="grid-column: 1 / -1">IRJ number (six digits)
                  <input
                    id="sage-registration-irj"
                    inputmode="numeric"
                    pattern="[0-9]{6}"
                    maxlength="6"
                    minlength="6"
                    autocomplete="off"
                    required
                  >
                </label>
              </div>
              <div id="sage-registration-error" class="error"></div>
              <div class="actions">
                <button type="button" class="secondary" id="sage-registration-cancel">Cancel</button>
                <button type="submit" class="primary">Confirm registration</button>
              </div>
            </form>
          </dialog>

          <dialog id="admin-edit-dialog" class="admin-edit-dialog">
            <form id="admin-edit-form" class="dialog-content">
              <h2 id="admin-edit-title">Edit record</h2>
              <p>Update the fields below, then save your changes.</p>
              <div id="admin-edit-fields" class="admin-edit-fields"></div>
              <div class="actions">
                <button type="button" class="secondary" id="admin-edit-cancel">Cancel</button>
                <button type="submit" class="primary">Save changes</button>
              </div>
            </form>
          </dialog>

          <div id="app-root" class="hidden">
          <aside class="sidebar">
            <div class="brand">
              <h1>Swantex</h1>
              <p>Invoice Processing</p>
            </div>
            <nav class="section-nav" id="section-nav">
              <button data-tab="search" class="active">Search invoices</button>
              <button data-tab="statements">Statements</button>
              <button data-tab="incoming">Incoming<span class="count" id="count-incoming">0</span></button>
              <button data-tab="needs-review">Flagged<span class="count" id="count-needs-review">0</span></button>
              <button data-tab="po-matching">PO Matching<span class="count" id="count-po-matching">0</span></button>
              <button data-tab="sage-registration">Sage Registration<span class="count" id="count-sage-registration">0</span></button>
              <button data-tab="approver1">Approver 1<span class="count" id="count-approver1">0</span></button>
              <button data-tab="approver2">Approver 2<span class="count" id="count-approver2">0</span></button>
              <button data-tab="on-hold">On Hold / Query<span class="count" id="count-on-hold">0</span></button>
              <button data-tab="approved">Approved<span class="count" id="count-approved">0</span></button>
              <button data-tab="payment-bacs">BACS<span class="count" id="count-payment-bacs">0</span></button>
              <button data-tab="payment-bankline">Bankline<span class="count" id="count-payment-bankline">0</span></button>
              <button data-tab="payment-foreign-poa">Foreign POA<span class="count" id="count-payment-foreign-poa">0</span></button>
              <button data-tab="reconciliation">Bank Reconciliation<span class="count" id="count-reconciliation">0</span></button>
              <button data-tab="complete">Complete / Filed<span class="count" id="count-complete">0</span></button>
              <button data-tab="rejected">Rejected<span class="count" id="count-rejected">0</span></button>
              <button data-tab="admin">Admin</button>
            </nav>
            <div class="header-controls">
              <span class="user-chip" id="user-chip">
                <span id="user-chip-label">Signed in</span>
                <button type="button" id="logout-button">Sign out</button>
              </span>
            </div>
          </aside>
          <main>
            <div class="tab-panel" data-tab-panel="incoming">
              <div class="layout">
                <section class="card" aria-labelledby="document-title">
                  <div class="card-header">
                    <h2 id="document-title">Invoice PDF</h2>
                    <span class="badge" id="document-badge">No invoice selected</span>
                  </div>
                  <div class="pdf-empty" id="pdf-empty">
                    <div>
                      <div class="pdf-icon">PDF</div>
                      <strong>No received invoice selected</strong>
                      <p>PDFs retrieved from Outlook will appear here after the webhook worker processes them.</p>
                    </div>
                  </div>
                  <iframe class="pdf-frame" id="pdf-frame" title="Selected invoice PDF"></iframe>
                </section>

                <section class="card" aria-labelledby="details-title">
                  <div class="card-header">
                    <h2 id="details-title">Extracted invoice information</h2>
                    <span class="badge" id="processing-badge">Awaiting invoice</span>
                  </div>
                  <div class="content">
                    <label class="invoice-picker">Received invoice
                      <select id="invoice-picker">
                        <option value="">No processed Outlook PDFs</option>
                      </select>
                    </label>
                    <details class="manual-upload">
                      <summary>Manually add an invoice to Incoming Invoices</summary>
                      <p class="manual-upload-hint">
                        For invoices received outside of the normal email process (e.g. post, hand-delivered,
                        or another mailbox). The PDF enters the exact same workflow as an Outlook-sourced invoice.
                      </p>
                      <form id="manual-upload-form">
                        <div class="grid">
                          <label style="grid-column: 1 / -1">Invoice PDF
                            <input type="file" id="manual-upload-file" accept="application/pdf" required>
                          </label>
                          <label>Sender name<input id="manual-upload-sender-name" placeholder="Optional"></label>
                          <label>Sender email<input id="manual-upload-sender-address" placeholder="Optional"></label>
                          <label style="grid-column: 1 / -1">Subject / description
                            <input id="manual-upload-subject" placeholder="Optional, e.g. 'Posted invoice from Acme Ltd'">
                          </label>
                        </div>
                        <div class="actions">
                          <button type="submit" class="primary" id="manual-upload-submit">Add to Incoming Invoices</button>
                        </div>
                        <div id="manual-upload-status"></div>
                      </form>
                    </details>

                    <h3 class="section-title">Source email</h3>
                    <div class="grid">
                      <label>Sender<input id="source-sender" disabled placeholder="Email sender"></label>
                      <label>Date received<input id="source-received" disabled placeholder="Received date and time"></label>
                      <label style="grid-column: 1 / -1">Email subject<input id="source-subject" disabled placeholder="Email subject"></label>
                    </div>

                    <h3 class="section-title">Invoice details (AI-extracted — review and correct before confirming)</h3>
                    <div class="grid">
                      <label>IRJ number
                        <input id="preview-irj" disabled placeholder="Assigned before final filing">
                      </label>
                      <label>Company being invoiced
                        <select id="preview-company">
                          <option value="">Select company…</option>
                        </select>
                      </label>
                      <label>Supplier
                        <select id="preview-supplier" disabled>
                          <option value="">Select a company first…</option>
                        </select>
                      </label>
                      <div class="notice supplier-match-prompt" id="supplier-match-prompt" style="display: none;">
                        <strong>Supplier check</strong>
                        <div>
                          <span id="supplier-match-text"></span>
                          <div class="supplier-match-actions">
                            <button type="button" id="confirm-supplier-match">Confirm suggested supplier</button>
                          </div>
                        </div>
                      </div>
                      <label>Supplier invoice number
                        <input id="preview-supplier-invoice" placeholder="Extracted invoice number">
                      </label>
                      <label>Purchase Order number
                        <input id="preview-po" placeholder="Enter a PO number if one applies">
                      </label>
                      <label>Invoice date
                        <input id="preview-invoice-date" type="date">
                      </label>
                      <label>Invoice value
                        <input id="preview-value" type="number" step="0.01" placeholder="0.00">
                      </label>
                      <label>Currency
                        <input id="preview-currency" value="GBP" placeholder="Currency">
                      </label>
                    </div>

                    <h3 class="section-title">AI extraction status</h3>
                    <div class="grid">
                      <label>AI processing status
                        <input id="processing-status" disabled placeholder="Waiting for invoice">
                      </label>
                      <label>Overall confidence
                        <div class="confidence">
                          <input id="ai-confidence" disabled placeholder="Not calculated">
                          <small id="ai-confidence-percent">—</small>
                        </div>
                      </label>
                      <label style="grid-column: 1 / -1">Review warnings
                        <textarea id="review-warnings" disabled placeholder="Missing, uncertain, or conflicting fields will be shown here."></textarea>
                      </label>
                      <label>Extraction model
                        <input id="extraction-model" disabled placeholder="Not recorded">
                      </label>
                      <label>Prompt / extraction version
                        <input id="extraction-prompt-version" disabled placeholder="Not recorded">
                      </label>
                      <label style="grid-column: 1 / -1">Routing explanation
                        <textarea id="routing-explanation" disabled placeholder="The routing decision will be shown here."></textarea>
                      </label>
                      <label style="grid-column: 1 / -1">Correction reason
                        <textarea id="correction-reason" placeholder="Describe why any extracted values were changed (optional)."></textarea>
                      </label>
                    </div>

                    <div class="notice" id="duplicate-warning" style="display: none; background: #fef2f2; border-color: #f5b5b5; color: #9b1c1c;">
                      <strong>⚠</strong>
                      <div id="duplicate-warning-text"></div>
                    </div>
                  </div>
                  <div class="actions">
                    <button class="danger" id="delete-invoice-button">Delete invoice</button>
                    <button class="secondary" id="flag-review-button">Flag for review</button>
                    <button class="danger" id="override-duplicate-button" style="display: none">This is not a duplicate — route anyway</button>
                    <button class="danger" id="cancel-duplicate-button" style="display: none">Confirmed duplicate — cancel</button>
                    <button class="secondary" id="retry-approval-route-button" style="display: none">Retry configured approver route</button>
                    <button class="primary" id="confirm-invoice-button">Purchase Ledger: confirm invoice</button>
                  </div>
                </section>
              </div>
            </div>

            <div class="tab-panel active" data-tab-panel="search">
              <section class="card">
                <div class="card-header"><h2>Search invoices</h2></div>
                <div class="content">
                  <form class="admin-form" id="invoice-search-form">
                    <select id="invoice-search-company" aria-label="Invoice company">
                      <option value="">All companies</option>
                    </select>
                    <select id="invoice-search-supplier" aria-label="Supplier">
                      <option value="">All suppliers</option>
                    </select>
                    <input
                      id="invoice-search-irj"
                      placeholder="IRJ number (optional)"
                      aria-label="IRJ number"
                      inputmode="numeric"
                      pattern="[0-9]{6}"
                      maxlength="6"
                      autocomplete="off"
                    >
                    <button type="submit" class="primary">Search</button>
                  </form>
                  <div id="invoice-search-result" class="empty-state">
                    Select a company or supplier, or enter an IRJ number.
                  </div>
                </div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="statements">
              <section class="card">
                <div class="card-header"><h2>Company statements</h2></div>
                <div class="content">
                  <p class="admin-help">
                    Statements are read directly from each SharePoint
                    <code>Statements/{Company}</code> folder.
                  </p>
                  <label>Company
                    <select id="statement-company">
                      <option value="">Select company…</option>
                    </select>
                  </label>
                  <div id="statement-list" class="empty-state">
                    Select a company to view its statements.
                  </div>
                </div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="needs-review">
              <section class="card">
                <div class="card-header"><h2>Flagged Documents — Purchase Ledger Review</h2></div>
                <div class="content" id="needs-review-table"></div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="po-matching">
              <section class="card">
                <div class="card-header"><h2>Purchase Order Invoice Matching</h2></div>
                <div class="content" id="po-matching-table"></div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="sage-registration">
              <section class="card">
                <div class="card-header"><h2>Awaiting Sage Registration</h2></div>
                <div class="content" id="sage-registration-table"></div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="approver1">
              <section class="card">
                <div class="card-header"><h2>Approver 1 — Pending Approvals</h2></div>
                <div class="content" id="approver1-table"></div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="approver2">
              <section class="card">
                <div class="card-header"><h2>Approver 2 — Pending Approvals</h2></div>
                <div class="content" id="approver2-table"></div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="on-hold">
              <section class="card">
                <div class="card-header"><h2>On Hold / Approval Queries</h2></div>
                <div class="content" id="on-hold-table"></div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="approved">
              <section class="card">
                <div class="card-header"><h2>Approved — Ready for Payment</h2></div>
                <div class="content" id="approved-table"></div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="payment-bacs">
              <section class="card">
                <div class="card-header"><h2>Approved for Payment — BACS</h2></div>
                <div class="content" id="payment-bacs-table"></div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="payment-bankline">
              <section class="card">
                <div class="card-header"><h2>Approved for Payment — Bankline</h2></div>
                <div class="content" id="payment-bankline-table"></div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="payment-foreign-poa">
              <section class="card">
                <div class="card-header"><h2>Approved for Payment — Foreign POA</h2></div>
                <div class="content" id="payment-foreign-poa-table"></div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="reconciliation">
              <section class="card">
                <div class="card-header"><h2>Bank Reconciliation</h2></div>
                <div class="content" id="reconciliation-table"></div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="complete">
              <section class="card">
                <div class="card-header"><h2>Complete / Filed</h2></div>
                <div class="content" id="complete-table"></div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="rejected">
              <section class="card">
                <div class="card-header"><h2>Rejected</h2></div>
                <div class="content" id="rejected-table"></div>
              </section>
            </div>

            <div class="tab-panel" data-tab-panel="admin">
              <section class="card">
                <div class="content" id="admin-panel">
                  <details class="admin-block admin-collapsible" id="admin-bi-metrics">
                    <summary>Reporting metrics</summary>
                    <div class="admin-collapsible-content">
                      <div class="metrics-toolbar">
                        <label>Invoice volume interval
                          <select id="metrics-granularity">
                            <option value="day">Daily — last 30 days</option>
                            <option value="week">Weekly — last 12 weeks</option>
                            <option value="month" selected>Monthly — last 12 months</option>
                          </select>
                        </label>
                      </div>
                      <div id="metrics-dashboard" class="empty-state">
                        Open this section to load metrics.
                      </div>
                    </div>
                  </details>

                  <details class="admin-block admin-collapsible" id="admin-operations">
                    <summary>Worker and production monitoring</summary>
                    <div class="admin-collapsible-content">
                      <button type="button" id="refresh-operations" class="secondary">Refresh status</button>
                      <div id="operations-dashboard" class="empty-state">
                        Open this section to load worker and queue status.
                      </div>
                    </div>
                  </details>

                  <details class="admin-block admin-collapsible">
                    <summary>IRJ numbering by company</summary>
                    <div class="admin-collapsible-content">
                      <p class="admin-help">
                        Automatic companies continue from the latest paper IRJ.
                        Manual companies require a six-digit IRJ during Sage registration.
                      </p>
                      <div id="admin-irj-settings"></div>
                    </div>
                  </details>

                  <div class="admin-block">
                    <h3>Bulk import supplier master data</h3>
                    <p class="admin-help">
                      Choose the invoice company, then upload an Excel (.xlsx) sheet
                      containing Trading Partner Name, Supplier Account Number,
                      Default Payment Method, Payment Terms, Bank Account, and
                      Approver(s). Optional Approver Email columns can also be
                      included. Every supplier and approval route will be assigned
                      to the selected company.
                    </p>
                    <form class="admin-form" id="admin-import-form">
                      <select id="admin-import-company" required>
                        <option value="">Select invoice company…</option>
                      </select>
                      <input id="admin-import-file" type="file" accept=".xlsx,.xlsm" required>
                      <button type="submit" class="primary">Import workbook</button>
                    </form>
                    <div id="admin-import-result"></div>
                  </div>

                  <details class="admin-block admin-collapsible" id="admin-companies-config">
                    <summary>Companies</summary>
                    <div class="admin-collapsible-content">
                      <p id="admin-sharepoint-folder-status" class="admin-help">
                        Open this section to load SharePoint folders.
                      </p>
                      <form class="admin-form" id="admin-company-form">
                        <input id="admin-company-name" placeholder="Company name" required>
                        <select id="admin-company-root-folder" required disabled>
                          <option value="">Select SharePoint company folder…</option>
                        </select>
                        <input id="admin-company-aliases" placeholder="Aliases (comma separated)">
                        <button type="submit" class="primary" id="admin-company-submit" disabled>Add company</button>
                      </form>
                      <div id="admin-company-folder-preview" class="empty">
                        Select a company folder to preview its workflow destinations.
                      </div>
                      <div id="admin-companies-table"></div>
                    </div>
                  </details>

                  <div class="admin-block">
                    <h3>Suppliers</h3>
                    <form class="admin-form" id="admin-supplier-form">
                      <input id="admin-supplier-name" placeholder="Supplier name" required>
                      <input id="admin-supplier-aliases" placeholder="Aliases (comma separated)">
                      <select id="admin-supplier-default-company">
                        <option value="">No default company</option>
                      </select>
                      <input id="admin-supplier-contact" placeholder="Contact email (optional)">
                      <input
                        id="admin-supplier-invoice-pattern"
                        placeholder="Invoice no. pattern, e.g. ########@@@"
                        title="# = digit, @ = letter, * = letter or digit"
                      >
                      <button type="submit" class="primary">Add supplier</button>
                    </form>
                    <div id="admin-suppliers-table"></div>
                  </div>

                  <div class="admin-block">
                    <h3>Supplier payment settings</h3>
                    <div id="admin-supplier-terms-table"></div>
                  </div>

                  <div class="admin-block">
                    <h3>Approval matrix</h3>
                    <div class="metrics-toolbar">
                      <label>Filter routes
                        <input id="admin-matrix-filter" placeholder="Company, supplier or approver">
                      </label>
                      <label>Show
                        <select id="admin-matrix-email-filter">
                          <option value="all">All routes</option>
                          <option value="missing">Missing approver email</option>
                        </select>
                      </label>
                    </div>
                    <form class="admin-form" id="admin-matrix-form">
                      <select id="admin-matrix-company" required>
                        <option value="">Select invoice company…</option>
                        <option value="*">All invoice companies</option>
                      </select>
                      <select id="admin-matrix-supplier" required>
                        <option value="">Select supplier company…</option>
                      </select>
                      <input id="admin-matrix-approver1-name" placeholder="Approver 1 name" required>
                      <input id="admin-matrix-approver1-email" type="email" placeholder="Approver 1 email" required>
                      <input id="admin-matrix-approver2-name" placeholder="Approver 2 name (optional)">
                      <input id="admin-matrix-approver2-email" type="email" placeholder="Approver 2 email (optional)">
                      <button type="submit" class="primary">Save entry</button>
                    </form>
                    <div id="admin-matrix-table"></div>
                  </div>

                  <div class="admin-block">
                    <h3>AI extraction confidence threshold</h3>
                    <form class="admin-form" id="admin-threshold-form">
                      <input id="admin-threshold-value" type="number" min="0" max="1" step="0.01" placeholder="0.80" style="max-width: 120px">
                      <button type="submit" class="primary">Save threshold</button>
                    </form>
                  </div>

                </div>
              </section>
            </div>
          </main>
          </div>
          <script>
            const SECTION_STATUSES = {
              "incoming": ["Awaiting AI Extraction"],
              "needs-review": ["Needs Review"],
              "po-matching": ["Awaiting PO Matching", "PO Query / Matching Issue"],
              "sage-registration": ["Awaiting Sage Registration"],
              "approver1": ["Awaiting Approval 1"],
              "approver2": ["Awaiting Approval 2"],
              "on-hold": [
                "Approval Query / On Hold",
                "Awaiting Approval 1",
                "Awaiting Approval 2",
              ],
              "approved": ["Approved"],
              "payment-bacs": ["Approved for Payment - BACS"],
              "payment-bankline": ["Approved for Payment - Bankline"],
              "payment-foreign-poa": ["Approved for Payment - Foreign POA", "Foreign Payment / Awaiting Allocation"],
              "reconciliation": ["Paid / Awaiting Bank Reconciliation"],
              "complete": ["Reconciled / Complete", "Statement Filed"],
              "rejected": ["Rejected", "Cancelled - Duplicate"],
            };
            // Which nav tabs each signed-in role may view. "admin" is a
            // config panel, not an invoice-status tab, and is only ever
            // shown to the admin role. Every other tab maps 1:1 onto
            // MANUAL_VS_AUTOMATED.md's manual decision steps.
            const ROLE_TABS = {
              "admin": ["search", "incoming", "statements", "needs-review", "po-matching", "sage-registration", "approver1", "approver2", "on-hold", "approved", "payment-bacs", "payment-bankline", "payment-foreign-poa", "reconciliation", "complete", "rejected", "admin"],
              "purchase_ledger": ["search", "incoming", "statements", "needs-review", "po-matching", "sage-registration", "on-hold", "approved", "payment-bacs", "payment-bankline", "payment-foreign-poa", "reconciliation", "complete", "rejected"],
              "approver1": ["search", "statements", "approver1"],
              "approver2": ["search", "statements", "approver2"],
              "purchasing": ["search", "statements", "po-matching"],
            };

            const picker = document.getElementById("invoice-picker");
            const pdfFrame = document.getElementById("pdf-frame");
            const pdfEmpty = document.getElementById("pdf-empty");
            const documentBadge = document.getElementById("document-badge");
            const processingBadge = document.getElementById("processing-badge");
            const toastContainer = document.getElementById("toast-container");
            const loginScreen = document.getElementById("login-screen");
            const appRoot = document.getElementById("app-root");
            let invoices = [];
            let displayedInvoiceId = null;
            const ACTIVE_TAB_STORAGE_KEY = "invoice-processor-active-tab";
            const SCROLL_STORAGE_PREFIX = "invoice-processor-scroll-";
            let currentTab = storedValue(ACTIVE_TAB_STORAGE_KEY) || "search";
            let lastActivityId = null;
            let currentUser = null;
            let authConfig = { microsoft_enabled: false, local_enabled: true };
            let invoiceCompanies = [];
            let sharePointCompanyFolders = new Map();
            let adminCompanies = [];
            let adminSuppliers = [];
            let adminMatrix = [];
            let adminSupplierTerms = [];
            let adminMatrixFilterTimer = null;
            let irjConfigurations = new Map();
            let supplierRequestSequence = 0;
            let invoiceRefreshSequence = 0;
            let renderedInvoiceSnapshot = null;
            let activityPollTimer = null;
            let invoicePollTimer = null;
            let metricsRefreshTimer = null;
            let activityEventSource = null;
            let openedFlaggedInvoiceId = null;
            const expandedInvoiceIds = new Set();

            function storedValue(key) {
              try {
                return window.localStorage.getItem(key);
              } catch (error) {
                return null;
              }
            }

            function storeValue(key, value) {
              try {
                window.localStorage.setItem(key, value);
              } catch (error) {
                // The app remains usable when browser storage is disabled.
              }
            }

            function activateTab(tab) {
              const button = document.querySelector(
                `#section-nav button[data-tab="${tab}"]`
              );
              if (!button || button.style.display === "none") return false;
              currentTab = tab;
              storeValue(ACTIVE_TAB_STORAGE_KEY, tab);
              document.querySelectorAll("#section-nav button").forEach(item => {
                item.classList.toggle("active", item === button);
              });
              document.querySelectorAll(".tab-panel").forEach(panel => {
                panel.classList.toggle("active", panel.dataset.tabPanel === tab);
              });
              const restoreScrollPosition = () => window.requestAnimationFrame(() => {
                const savedPosition = Number(
                  storedValue(`${SCROLL_STORAGE_PREFIX}${tab}`) || 0
                );
                window.scrollTo(0, Number.isFinite(savedPosition) ? savedPosition : 0);
              });
              if (tab === "statements") {
                loadStatements().finally(restoreScrollPosition);
              } else {
                restoreScrollPosition();
              }
              return true;
            }

            function setValue(id, value) {
              const el = document.getElementById(id);
              if (el) el.value = value || "";
            }

            function applyPreviewConfidence(invoice) {
              const confidences = fieldConfidences(invoice);
              const fields = {
                "preview-company": "company",
                "preview-supplier": "supplier",
                "preview-supplier-invoice": "supplier_invoice_number",
                "preview-po": "purchase_order_number",
                "preview-invoice-date": "invoice_date",
                "preview-value": "invoice_value",
                "preview-currency": "currency",
              };
              for (const [elementId, fieldName] of Object.entries(fields)) {
                const element = document.getElementById(elementId);
                if (!element) continue;
                const confidence = confidences[fieldName];
                element.title = confidence == null
                  ? "No field confidence was recorded."
                  : `AI confidence: ${Math.round(Number(confidence) * 100)}%`;
                element.style.outline = confidence != null && Number(confidence) < 0.8
                  ? "2px solid #d97706"
                  : "";
              }
            }

            function showToast(message, isError) {
              const toast = document.createElement("div");
              toast.className = "toast" + (isError ? " error" : "");
              toast.textContent = message;
              toastContainer.appendChild(toast);
              setTimeout(() => toast.remove(), 5000);
            }

            async function showInvoice(invoice) {
              documentBadge.textContent = invoice.original_filename;
              processingBadge.textContent = invoice.status;
              setValue("source-sender", invoice.sender_address || invoice.sender_name);
              setValue("source-received", invoice.received_at);
              setValue("source-subject", invoice.subject);
              setValue("processing-status", invoice.status);
              const confidence = invoice.ai_confidence;
              setValue(
                "ai-confidence",
                confidence == null ? "" : Number(confidence).toFixed(2)
              );
              document.getElementById("ai-confidence-percent").textContent =
                confidence == null ? "—" : `${Math.round(Number(confidence) * 100)}%`;
              setValue("preview-irj", invoice.irj_number);
              setValue(
                "review-warnings",
                invoice.ai_review_warnings || invoice.review_reason
              );
              setValue("extraction-model", invoice.extraction_model);
              setValue("extraction-prompt-version", invoice.extraction_prompt_version);
              setValue("routing-explanation", invoice.routing_explanation);
              const duplicateWarning = document.getElementById("duplicate-warning");
              const overrideButton = document.getElementById("override-duplicate-button");
              const cancelDuplicateButton = document.getElementById("cancel-duplicate-button");
              const deleteInvoiceButton = document.getElementById("delete-invoice-button");
              const retryApprovalRouteButton = document.getElementById("retry-approval-route-button");
              if (invoice.duplicate_of_invoice_id) {
                duplicateWarning.style.display = "grid";
                document.getElementById("duplicate-warning-text").textContent =
                  invoice.review_reason ||
                  `Possible duplicate of invoice #${invoice.duplicate_of_invoice_id}.`;
                overrideButton.style.display = "";
                cancelDuplicateButton.style.display = "";
              } else {
                duplicateWarning.style.display = "none";
                overrideButton.style.display = "none";
                cancelDuplicateButton.style.display = "none";
              }
              retryApprovalRouteButton.style.display =
                invoice.status === "Needs Review" &&
                invoice.invoice_type === "nominal" &&
                invoice.sage_registered_at
                  ? ""
                  : "none";
              deleteInvoiceButton.disabled = ![
                "Awaiting AI Extraction",
                "Needs Review",
                "Awaiting PO Matching",
                "PO Query / Matching Issue",
                "Awaiting Sage Registration",
              ].includes(invoice.status);
              if (displayedInvoiceId !== invoice.id) {
                // Only (re)populate editable AI fields when the
                // displayed invoice actually changes. showInvoice() is also
                // called on every periodic refresh (loadInvoices runs every
                // 5s); without this guard it would keep stomping on
                // whatever the user is actively typing into these fields
                // with the invoice's last-saved (often blank) values.
                setValue("preview-company", invoice.company || "");
                displayedInvoiceId = invoice.id;
                await loadSuppliers(invoice.company || "", invoice.supplier || "");
                if (displayedInvoiceId !== invoice.id) return;
                setValue("preview-supplier-invoice", invoice.supplier_invoice_number);
                setValue("preview-po", invoice.po_number);
                setValue("preview-invoice-date", invoice.invoice_date);
                setValue("preview-value", invoice.invoice_value);
                setValue("preview-currency", invoice.currency || "GBP");
                setValue("correction-reason", invoice.correction_reason);
                applyPreviewConfidence(invoice);
                pdfFrame.src = `/api/invoices/${invoice.id}/pdf`;
              }
              pdfFrame.style.display = "block";
              pdfEmpty.style.display = "none";
            }

            function isFlaggedInvoice(invoice) {
              return invoice.status === "Needs Review";
            }

            function incomingInvoices() {
              return invoices.filter(i =>
                i.document_type !== "statement" &&
                SECTION_STATUSES["incoming"].includes(i.status)
              );
            }

            function clearInvoicePreview() {
              picker.innerHTML = '<option value="">No invoices awaiting review</option>';
              displayedInvoiceId = null;
              documentBadge.textContent = "No invoice selected";
              processingBadge.textContent = "Awaiting invoice";
              [
                "source-sender",
                "source-received",
                "source-subject",
                "processing-status",
                "ai-confidence",
                "preview-irj",
                "review-warnings",
                "extraction-model",
                "extraction-prompt-version",
                "routing-explanation",
                "correction-reason",
                "preview-company",
                "preview-supplier-invoice",
                "preview-po",
                "preview-invoice-date",
                "preview-value",
              ].forEach(id => setValue(id, ""));
              setValue("preview-currency", "GBP");
              document.getElementById("ai-confidence-percent").textContent = "—";
              document.getElementById("preview-supplier").innerHTML =
                '<option value="">Select a company first…</option>';
              document.getElementById("preview-supplier").disabled = true;
              document.getElementById("duplicate-warning").style.display = "none";
              document.getElementById("override-duplicate-button").style.display = "none";
              document.getElementById("cancel-duplicate-button").style.display = "none";
              document.getElementById("retry-approval-route-button").style.display = "none";
              document.getElementById("delete-invoice-button").disabled = true;
              document.getElementById("supplier-match-prompt").style.display = "none";
              document.getElementById("confirm-supplier-match").dataset.supplier = "";
              pdfFrame.removeAttribute("src");
              pdfFrame.style.display = "none";
              pdfEmpty.style.display = "grid";
            }

            async function refreshPicker() {
              const candidates = incomingInvoices();
              const openedFlaggedInvoice = invoices.find(
                invoice =>
                  String(invoice.id) === String(openedFlaggedInvoiceId) &&
                  isFlaggedInvoice(invoice) &&
                  invoice.document_type === "invoice"
              );
              if (openedFlaggedInvoice) {
                candidates.unshift(openedFlaggedInvoice);
              } else {
                openedFlaggedInvoiceId = null;
              }
              if (!candidates.length) {
                clearInvoicePreview();
                return;
              }
              const selectedId = picker.value;
              picker.innerHTML = "";
              for (const invoice of candidates) {
                const option = document.createElement("option");
                option.value = invoice.id;
                option.textContent =
                  `${openedFlaggedInvoice === invoice ? "[Flagged] " : ""}` +
                  `${invoice.original_filename} — ${invoice.subject || "No subject"}`;
                picker.appendChild(option);
              }
              const selected = candidates.find(
                invoice => String(invoice.id) === String(openedFlaggedInvoiceId)
              ) || candidates.find(
                invoice => String(invoice.id) === selectedId
              ) || candidates[0];
              picker.value = String(selected.id);
              await showInvoice(selected);
            }

            picker.addEventListener("change", async () => {
              const selected = invoices.find(
                invoice => String(invoice.id) === picker.value
              );
              if (selected) await showInvoice(selected);
            });

            async function loadInvoices() {
              const requestSequence = ++invoiceRefreshSequence;
              try {
                const response = await fetch(
                  "/api/invoices?limit=500",
                  { cache: "no-store" }
                );
                if (!response.ok) {
                  showToast("Received invoices could not be loaded.", true);
                  return;
                }
                const refreshedInvoices = await response.json();
                if (requestSequence !== invoiceRefreshSequence) return;
                const refreshedSnapshot = JSON.stringify(refreshedInvoices);
                if (refreshedSnapshot === renderedInvoiceSnapshot) return;
                renderedInvoiceSnapshot = refreshedSnapshot;
                invoices = refreshedInvoices;
                renderAllSections();
                updateCounts();
                await refreshPicker();
              } catch (error) {
                showToast(
                  "Received invoices could not be loaded. Retrying automatically.",
                  true
                );
              }
            }

            async function loadCompanies() {
              const response = await fetch("/api/companies");
              if (!response.ok) return;
              const companies = await response.json();
              invoiceCompanies = companies;
              const select = document.getElementById("preview-company");
              select.innerHTML = '<option value="">Select company…</option>';
              const searchSelect = document.getElementById("invoice-search-company");
              searchSelect.innerHTML = '<option value="">All companies</option>';
              for (const company of companies) {
                const option = document.createElement("option");
                option.value = company.name;
                option.textContent = company.name;
                select.appendChild(option);
                const searchOption = option.cloneNode(true);
                searchSelect.appendChild(searchOption);
              }
            }

            async function loadSearchSuppliers(company = "") {
              const select = document.getElementById("invoice-search-supplier");
              const response = await fetch(
                company
                  ? `/api/suppliers?company=${encodeURIComponent(company)}`
                  : "/api/suppliers"
              );
              if (!response.ok) return;
              const suppliers = await response.json();
              select.innerHTML = '<option value="">All suppliers</option>';
              for (const supplier of suppliers) {
                const option = document.createElement("option");
                option.value = supplier.name;
                option.textContent = supplier.name;
                select.appendChild(option);
              }
            }

            document.getElementById("invoice-search-company").addEventListener(
              "change",
              event => loadSearchSuppliers(event.target.value)
            );

            async function loadSuppliers(
              company = document.getElementById("preview-company").value,
              selectedSupplier = document.getElementById("preview-supplier").value
            ) {
              const requestSequence = ++supplierRequestSequence;
              const select = document.getElementById("preview-supplier");
              if (!company) {
                select.innerHTML = '<option value="">Select a company first…</option>';
                select.disabled = true;
                hideSupplierMatchPrompt();
                return;
              }
              const response = await fetch(
                `/api/suppliers?company=${encodeURIComponent(company)}`
              );
              if (!response.ok) return;
              const suppliers = await response.json();
              if (requestSequence !== supplierRequestSequence) return;
              select.disabled = false;
              select.innerHTML = '<option value="">Select supplier…</option>';
              for (const supplier of suppliers) {
                const option = document.createElement("option");
                option.value = supplier.name;
                option.textContent = supplier.name;
                select.appendChild(option);
              }
              const configuredSupplier = suppliers.find(supplier =>
                supplier.name.localeCompare(
                  selectedSupplier,
                  undefined,
                  { sensitivity: "accent" }
                ) === 0 ||
                (supplier.aliases || []).some(alias =>
                  alias.localeCompare(
                    selectedSupplier,
                    undefined,
                    { sensitivity: "accent" }
                  ) === 0
                )
              );
              if (configuredSupplier) {
                select.value = configuredSupplier.name;
                hideSupplierMatchPrompt();
              } else if (selectedSupplier) {
                select.value = "";
                showSupplierMatchPrompt(selectedSupplier, company, suppliers);
              } else {
                select.value = "";
                hideSupplierMatchPrompt();
              }
            }

            document.getElementById("preview-company").addEventListener(
              "change",
              event => loadSuppliers(event.target.value, "")
            );
            document.getElementById("preview-supplier").addEventListener(
              "change",
              event => {
                if (event.target.value) hideSupplierMatchPrompt();
              }
            );

            function normalizedSupplierName(value) {
              return String(value || "")
                .toLocaleLowerCase()
                .replace(/&/g, " and ")
                .replace(/\\b(limited|ltd|plc|inc|llc|company|co)\\b/g, "")
                .replace(/[^a-z0-9]/g, "");
            }

            function supplierNameSimilarity(left, right) {
              const a = normalizedSupplierName(left);
              const b = normalizedSupplierName(right);
              if (!a || !b) return 0;
              if (a === b) return 1;
              if (a.length < 2 || b.length < 2) return 0;
              const pairs = value => {
                const counts = new Map();
                for (let index = 0; index < value.length - 1; index += 1) {
                  const pair = value.slice(index, index + 2);
                  counts.set(pair, (counts.get(pair) || 0) + 1);
                }
                return counts;
              };
              const aPairs = pairs(a);
              const bPairs = pairs(b);
              let overlap = 0;
              for (const [pair, count] of aPairs) {
                overlap += Math.min(count, bPairs.get(pair) || 0);
              }
              return (2 * overlap) / (a.length + b.length - 2);
            }

            function suggestedSupplier(extractedSupplier, suppliers) {
              let best = null;
              let bestScore = 0;
              for (const supplier of suppliers) {
                for (const candidate of [supplier.name, ...(supplier.aliases || [])]) {
                  const score = supplierNameSimilarity(extractedSupplier, candidate);
                  if (score > bestScore) {
                    best = supplier;
                    bestScore = score;
                  }
                }
              }
              return bestScore >= 0.62 ? best : null;
            }

            function hideSupplierMatchPrompt() {
              const prompt = document.getElementById("supplier-match-prompt");
              prompt.style.display = "none";
              document.getElementById("confirm-supplier-match").dataset.supplier = "";
            }

            function showSupplierMatchPrompt(extractedSupplier, company, suppliers) {
              const prompt = document.getElementById("supplier-match-prompt");
              const text = document.getElementById("supplier-match-text");
              const confirm = document.getElementById("confirm-supplier-match");
              const suggestion = suggestedSupplier(extractedSupplier, suppliers);
              if (suggestion) {
                text.textContent =
                  `We extracted “${extractedSupplier}”. Is this “${suggestion.name}” for ${company}?`;
                confirm.textContent = `Yes — use ${suggestion.name}`;
                confirm.dataset.supplier = suggestion.name;
                confirm.style.display = "";
              } else {
                text.textContent =
                  `“${extractedSupplier}” is not registered for ${company}. Select the correct supplier above or ask an administrator to add it.`;
                confirm.dataset.supplier = "";
                confirm.style.display = "none";
              }
              prompt.style.display = "grid";
            }

            document.getElementById("confirm-supplier-match").addEventListener(
              "click",
              event => {
                const supplier = event.currentTarget.dataset.supplier;
                if (!supplier) return;
                document.getElementById("preview-supplier").value = supplier;
                hideSupplierMatchPrompt();
                showToast(`Supplier confirmed as ${supplier}.`);
              }
            );

            function updateCounts() {
              for (const tab of Object.keys(SECTION_STATUSES)) {
                const count = tab === "incoming"
                  ? incomingInvoices().length
                  : invoices.filter(i =>
                      SECTION_STATUSES[tab].includes(i.status) &&
                      (tab !== "on-hold" || i.status === "Approval Query / On Hold" || i.hold_level)
                    ).length;
                const el = document.getElementById(`count-${tab}`);
                if (el) el.textContent = String(count);
              }
            }

            function escapeHtml(value) {
              const div = document.createElement("div");
              div.textContent = value == null ? "" : String(value);
              return div.innerHTML;
            }

            function fieldConfidences(invoice) {
              if (!invoice.ai_field_confidences) return {};
              try {
                const parsed = JSON.parse(invoice.ai_field_confidences);
                return parsed && typeof parsed === "object" ? parsed : {};
              } catch (error) {
                return {};
              }
            }

            function analysisValue(value) {
              return value === null || value === undefined || value === ""
                ? "—"
                : escapeHtml(value);
            }

            function formatUkTimestamp(value) {
              if (!value) return "—";
              const parsed = new Date(value);
              if (Number.isNaN(parsed.getTime())) return value;
              return parsed.toLocaleString("en-GB", {
                timeZone: "Europe/London",
                day: "2-digit",
                month: "2-digit",
                year: "numeric",
                hour: "2-digit",
                minute: "2-digit",
                timeZoneName: "short",
              });
            }

            function analysisField(label, value, confidence = null) {
              const confidenceBadge = confidence === null || confidence === undefined
                ? ""
                : `<span class="field-confidence">${Math.round(Number(confidence) * 100)}%</span>`;
              return `<dt>${escapeHtml(label)}</dt><dd>${analysisValue(value)}${confidenceBadge}</dd>`;
            }

            function invoiceHistoryHtml(invoice) {
              const entries = [];
              if (invoice.review_reason) {
                entries.push(["Flagged / review", invoice.review_reason]);
              }
              if (invoice.po_query_notes) {
                const context = [
                  invoice.po_query_category && `Category: ${invoice.po_query_category}`,
                  invoice.po_query_contact && `Contact: ${invoice.po_query_contact}`,
                ].filter(Boolean).join(" · ");
                entries.push([
                  "PO query",
                  `${invoice.po_query_notes}${context ? `\\n${context}` : ""}`,
                ]);
              }
              if (invoice.hold_reason) {
                entries.push(["Approval query / on hold", invoice.hold_reason]);
              }
              if (
                invoice.approver1_comments &&
                invoice.approver1_comments !== invoice.hold_reason
              ) {
                entries.push([
                  `${invoice.approver1_name || "Approval"} note`,
                  invoice.approver1_comments,
                ]);
              }
              if (
                invoice.approver2_comments &&
                invoice.approver2_comments !== invoice.hold_reason
              ) {
                entries.push([
                  `${invoice.approver2_name || "Approval"} note`,
                  invoice.approver2_comments,
                ]);
              }
              if (invoice.reconciliation_notes) {
                entries.push(["Reconciliation note", invoice.reconciliation_notes]);
              }
              if (invoice.rejection_reason) {
                entries.push(["Rejection reason", invoice.rejection_reason]);
              }
              if (invoice.cancellation_reason) {
                entries.push(["Cancellation reason", invoice.cancellation_reason]);
              }
              if (!entries.length) {
                return '<div class="mini-description">No queries or notes recorded.</div>';
              }
              return `<div class="analysis-history">${entries.map(([label, value]) =>
                `<div class="analysis-history-item"><strong>${escapeHtml(label)}</strong>${escapeHtml(value)}</div>`
              ).join("")}</div>`;
            }

            function invoiceAuditHtml(invoice) {
              const events = Array.isArray(invoice.audit_trail)
                ? invoice.audit_trail
                : [];
              if (!events.length) {
                return '<div class="mini-description">No audit events recorded.</div>';
              }
              return `<div class="analysis-history">${events.map(event => {
                const timestamp = event.created_at
                  ? formatUkTimestamp(event.created_at)
                  : "Unknown time";
                return `<div class="analysis-history-item">` +
                  `<strong>${escapeHtml(timestamp)} · ${escapeHtml(event.event_type)}</strong>` +
                  `${escapeHtml(event.message)}</div>`;
              }).join("")}</div>`;
            }

            function correctionFeedbackHtml(invoice) {
              if (!invoice.corrected_fields_json) return "";
              try {
                const corrections = JSON.parse(invoice.corrected_fields_json);
                const rows = Object.entries(corrections).map(([field, values]) =>
                  `<div class="analysis-history-item"><strong>${escapeHtml(field.replaceAll("_", " "))}</strong>` +
                  `${analysisValue(values.original)} → ${analysisValue(values.corrected)}</div>`
                ).join("");
                return `<h4>Human correction feedback</h4><div class="analysis-history">${rows}</div>`;
              } catch (error) {
                return "";
              }
            }

            function invoiceAnalysisHtml(invoice) {
              const confidences = fieldConfidences(invoice);
              const overallConfidence = invoice.ai_confidence === null ||
                  invoice.ai_confidence === undefined
                ? "—"
                : `${Math.round(Number(invoice.ai_confidence) * 100)}%`;
              const warnings = invoice.ai_review_warnings || invoice.review_reason;
              return `
                <div class="invoice-analysis">
                  <iframe
                    class="invoice-analysis-pdf"
                    src="/api/invoices/${invoice.id}/pdf"
                    title="${invoice.document_type === "statement" ? "Statement" : "Invoice"} ${escapeHtml(invoice.irj_number || invoice.original_filename)} PDF"
                  ></iframe>
                  <section class="invoice-analysis-fields" aria-label="Invoice analysis">
                    <h3>${invoice.document_type === "statement" ? "Statement classification" : "Extracted invoice analysis"}</h3>
                    <h4>Workflow</h4>
                    <dl class="invoice-analysis-list">
                      ${analysisField("Status", invoice.status)}
                      ${analysisField("Document type", invoice.document_type || "invoice")}
                      ${analysisField(
                        "Classification confidence",
                        invoice.document_classification_confidence == null
                          ? null
                          : `${Math.round(Number(invoice.document_classification_confidence) * 100)}%`
                      )}
                      ${analysisField("IRJ number", invoice.irj_number)}
                      ${analysisField("Invoice type", invoice.invoice_type)}
                      ${analysisField("Overall confidence", overallConfidence)}
                      ${analysisField("Extraction model", invoice.extraction_model)}
                      ${analysisField("Extraction version", invoice.extraction_prompt_version)}
                      ${analysisField("Routing explanation", invoice.routing_explanation)}
                    </dl>
                    <h4>Extracted fields</h4>
                    <dl class="invoice-analysis-list">
                      ${analysisField("Company", invoice.company, confidences.company)}
                      ${analysisField("Supplier", invoice.supplier, confidences.supplier)}
                      ${analysisField("Supplier invoice no.", invoice.supplier_invoice_number, confidences.supplier_invoice_number)}
                      ${analysisField("PO number", invoice.po_number, confidences.purchase_order_number)}
                      ${analysisField("Invoice date", invoice.invoice_date, confidences.invoice_date)}
                      ${analysisField("Invoice value", invoice.invoice_value, confidences.invoice_value)}
                      ${analysisField("Currency", invoice.currency, confidences.currency)}
                    </dl>
                    ${correctionFeedbackHtml(invoice)}
                    ${invoice.reviewed_by ? `<dl class="invoice-analysis-list">
                      ${analysisField("Reviewed by", invoice.reviewed_by)}
                      ${analysisField("Reviewed at", formatUkTimestamp(invoice.reviewed_at))}
                      ${analysisField("Correction reason", invoice.correction_reason)}
                    </dl>` : ""}
                    <h4>Source</h4>
                    <dl class="invoice-analysis-list">
                      ${analysisField("Filename", invoice.original_filename)}
                      ${analysisField("Sender", invoice.sender_address || invoice.sender_name)}
                      ${analysisField("Received", formatUkTimestamp(invoice.received_at))}
                      ${analysisField("Subject", invoice.subject)}
                    </dl>
                    ${invoice.approver1_name ? `
                      <h4>Processing</h4>
                      <dl class="invoice-analysis-list">
                        ${analysisField("Approver 1", invoice.approver1_name)}
                        ${analysisField("Approver 1 decision", invoice.approver1_decision)}
                        ${analysisField("Approver 1 date", formatUkTimestamp(invoice.approver1_date))}
                        ${analysisField("Approver 2", invoice.approver2_name)}
                        ${analysisField("Approver 2 decision", invoice.approver2_decision)}
                        ${analysisField("Approver 2 date", formatUkTimestamp(invoice.approver2_date))}
                      </dl>
                    ` : ""}
                    ${invoice.payment_date || invoice.reconciliation_date || invoice.reconciled_at ? `
                      <h4>Payment and reconciliation</h4>
                      <dl class="invoice-analysis-list">
                        ${analysisField("Payment date", invoice.payment_date)}
                        ${analysisField("Payment method", invoice.payment_method)}
                        ${analysisField("Payment reference", invoice.payment_reference)}
                        ${analysisField("Paid by", invoice.paid_by)}
                        ${analysisField("Bank statement date", invoice.reconciliation_date)}
                        ${analysisField("Marked reconciled", formatUkTimestamp(invoice.reconciled_at))}
                        ${analysisField("Reconciled by", invoice.reconciled_by)}
                      </dl>
                    ` : ""}
                    <h4>Description and history</h4>
                    ${invoiceHistoryHtml(invoice)}
                    ${Array.isArray(invoice.audit_trail) ? `
                      <h4>Audit trail</h4>
                      ${invoiceAuditHtml(invoice)}
                    ` : ""}
                    ${warnings ? `<div class="analysis-warning">${escapeHtml(warnings)}</div>` : ""}
                  </section>
                </div>
              `;
            }

            function renderSectionTable(
              containerId,
              statuses,
              columns,
              actionsFn,
              rowFilter = () => true
            ) {
              const container = document.getElementById(containerId);
              if (!container) return;
              const rows = invoices.filter(
                i => statuses.includes(i.status) && rowFilter(i)
              );
              if (!rows.length) {
                container.innerHTML = '<div class="empty-state">No documents in this section.</div>';
                return;
              }
              let html = '<table class="section-table"><thead><tr>';
              for (const column of columns) html += `<th>${column.label}</th>`;
              html += "<th>Actions</th></tr></thead><tbody>";
              for (const invoice of rows) {
                const expanded = expandedInvoiceIds.has(String(invoice.id));
                html += `<tr class="invoice-summary-row" data-expand-invoice="${invoice.id}" ` +
                  `tabindex="0" aria-expanded="${expanded}" title="Click to view invoice analysis">`;
                for (const column of columns) {
                  const value = escapeHtml(column.value(invoice));
                  html += column.boxed
                    ? `<td class="invoice-description-cell"><div class="mini-description">${value}</div></td>`
                    : `<td>${value}</td>`;
                }
                html += `<td class="row-actions"><div class="row-action-buttons">` +
                  `${actionsFn(invoice)}</div>` +
                  `<span class="invoice-expand-hint">${expanded ? "Hide" : "View"} PDF and extracted fields</span></td>`;
                html += "</tr>";
                if (expanded) {
                  html += `<tr class="invoice-analysis-row"><td colspan="${columns.length + 1}" ` +
                    `class="invoice-analysis-cell">${invoiceAnalysisHtml(invoice)}</td></tr>`;
                }
              }
              html += "</tbody></table>";
              container.innerHTML = html;
              container.querySelectorAll("[data-action]").forEach(button => {
                button.addEventListener("click", () => handleRowAction(button));
              });
              populateStatementCompanySelects(container);
              container.querySelectorAll("[data-expand-invoice]").forEach(row => {
                const toggle = event => {
                  if (event.target.closest("button, a, input, select, textarea")) return;
                  if (event.type === "keydown" && !["Enter", " "].includes(event.key)) return;
                  event.preventDefault();
                  const id = String(row.dataset.expandInvoice);
                  if (expandedInvoiceIds.has(id)) {
                    expandedInvoiceIds.delete(id);
                  } else {
                    expandedInvoiceIds.add(id);
                  }
                  renderAllSections();
                };
                row.addEventListener("click", toggle);
                row.addEventListener("keydown", toggle);
              });
            }

            function invoiceDescription(invoice) {
              const notes = [];
              if (invoice.status === "Needs Review") {
                notes.push(`Flagged: ${invoice.review_reason || invoice.ai_review_warnings || "Requires review"}`);
              }
              if (invoice.po_query_notes) {
                const details = [invoice.po_query_category, invoice.po_query_contact]
                  .filter(Boolean)
                  .join(" · ");
                notes.push(`PO query: ${invoice.po_query_notes}${details ? ` (${details})` : ""}`);
              }
              if (invoice.hold_reason) {
                notes.push(`Approval query: ${invoice.hold_reason}`);
              }
              if (
                invoice.approver1_comments &&
                invoice.approver1_comments !== invoice.hold_reason
              ) {
                notes.push(
                  `${invoice.approver1_name || "Approval"}: ${invoice.approver1_comments}`
                );
              }
              if (
                invoice.approver2_comments &&
                invoice.approver2_comments !== invoice.hold_reason
              ) {
                notes.push(
                  `${invoice.approver2_name || "Approval"}: ${invoice.approver2_comments}`
                );
              }
              if (invoice.rejection_reason) {
                notes.push(`Rejected: ${invoice.rejection_reason}`);
              }
              if (invoice.cancellation_reason) {
                notes.push(`Cancelled: ${invoice.cancellation_reason}`);
              }
              if (invoice.reconciliation_notes) {
                notes.push(`Reconciliation: ${invoice.reconciliation_notes}`);
              }
              return notes.length ? notes.join("\\n\\n") : "—";
            }

            const BASE_COLUMNS = [
              { label: "Type", value: i => i.document_type === "statement" ? "Statement" : "Invoice" },
              { label: "IRJ", value: i => i.irj_number || "—" },
              { label: "Company", value: i => i.company || "—" },
              { label: "Supplier", value: i => i.supplier || "—" },
              { label: "File", value: i => i.original_filename },
              { label: "Status", value: i => i.status },
              { label: "Description", value: invoiceDescription, boxed: true },
            ];

            function pdfLinkButton(invoice) {
              return `<a class="action-link" href="/api/invoices/${invoice.id}/pdf" ` +
                `target="_blank" rel="noopener">View PDF</a>`;
            }

            document.getElementById("invoice-search-form").addEventListener(
              "submit",
              async event => {
                event.preventDefault();
                const query = document.getElementById("invoice-search-irj").value.trim();
                const company = document.getElementById("invoice-search-company").value;
                const supplier = document.getElementById("invoice-search-supplier").value;
                const result = document.getElementById("invoice-search-result");
                result.className = "empty-state";
                result.textContent = "Searching…";
                try {
                  if (!query && !company && !supplier) {
                    throw new Error("Select a company or supplier, or enter an IRJ number.");
                  }
                  if (query && (query.length !== 6 || !/^\\d+$/.test(query))) {
                    throw new Error("An IRJ number must contain exactly six digits.");
                  }
                  const params = new URLSearchParams();
                  if (query) params.set("irj_number", query);
                  if (company) params.set("company", company);
                  if (supplier) params.set("supplier", supplier);
                  const response = await fetch(
                    `/api/invoice-search/filter?${params}`
                  );
                  if (!response.ok) {
                    const body = await response.json().catch(() => ({}));
                    throw new Error(body.detail || "Invoice search failed.");
                  }
                  const matches = await response.json();
                  if (!matches.length) {
                    result.textContent = "No matching invoices were found.";
                    return;
                  }
                  result.className = "";
                  result.innerHTML = matches.map(invoice =>
                    `<div class="metric-panel"><div class="actions">${pdfLinkButton(invoice)}</div>` +
                    `${invoiceAnalysisHtml(invoice)}</div>`
                  ).join("");
                } catch (error) {
                  result.textContent = error.message;
                  showToast(error.message, true);
                }
              }
            );

            let statementLibrary = {};

            function renderStatementList(company) {
              const container = document.getElementById("statement-list");
              if (!company) {
                container.className = "empty-state";
                container.textContent = "Select a company to view its statements.";
                return;
              }
              const statements = statementLibrary[company] || [];
              if (!statements.length) {
                container.className = "empty-state";
                container.textContent = `No PDF statements were found for ${company}.`;
                return;
              }
              container.className = "";
              container.innerHTML =
                '<table class="section-table"><thead><tr>' +
                "<th>Statement</th><th>Modified</th><th>Size</th><th>Action</th>" +
                "</tr></thead><tbody>" +
                statements.map(statement => `
                  <tr>
                    <td>${escapeHtml(statement.name)}</td>
                    <td>${escapeHtml(statement.lastModifiedDateTime || statement.createdDateTime || "—")}</td>
                    <td>${statement.size == null ? "—" : `${Math.ceil(Number(statement.size) / 1024)} KB`}</td>
                    <td><a class="action-link" href="/api/statements/${encodeURIComponent(statement.id)}/pdf" target="_blank" rel="noopener">View PDF</a></td>
                  </tr>
                `).join("") +
                "</tbody></table>";
            }

            async function loadStatements() {
              const companySelect = document.getElementById("statement-company");
              const container = document.getElementById("statement-list");
              container.className = "empty-state";
              container.textContent = "Loading statements from SharePoint…";
              const response = await fetch("/api/statements");
              if (!response.ok) {
                const body = await response.json().catch(() => ({}));
                container.textContent =
                  body.detail || "Statements could not be loaded from SharePoint.";
                return;
              }
              statementLibrary = await response.json();
              const selected = companySelect.value;
              companySelect.innerHTML = '<option value="">Select company…</option>';
              for (const company of Object.keys(statementLibrary)) {
                const option = document.createElement("option");
                option.value = company;
                option.textContent = company;
                companySelect.appendChild(option);
              }
              companySelect.value = Object.hasOwn(statementLibrary, selected)
                ? selected
                : "";
              renderStatementList(companySelect.value);
            }

            document.getElementById("statement-company").addEventListener(
              "change",
              event => renderStatementList(event.target.value)
            );

            function populateStatementCompanySelects(container) {
              container.querySelectorAll("[data-statement-company]").forEach(select => {
                const placeholder = document.createElement("option");
                placeholder.value = "";
                placeholder.textContent = "Select company…";
                select.appendChild(placeholder);
                for (const company of invoiceCompanies) {
                  const option = document.createElement("option");
                  option.value = company.name;
                  option.textContent = company.name;
                  select.appendChild(option);
                }
              });
            }

            function formatMetricMoney(value, currency) {
              try {
                return new Intl.NumberFormat("en-GB", {
                  style: "currency",
                  currency: currency || "GBP",
                }).format(Number(value));
              } catch (error) {
                return `${currency || "GBP"} ${Number(value).toFixed(2)}`;
              }
            }

            function metricValueTable(rows, firstHeading) {
              if (!rows.length) return '<div class="empty-state">No matching invoices.</div>';
              return '<table class="section-table"><thead><tr>' +
                `<th>${escapeHtml(firstHeading)}</th><th>Invoices</th><th>Value</th>` +
                '</tr></thead><tbody>' +
                rows.map(row => `<tr><td>${escapeHtml(row.name)}</td>` +
                  `<td>${escapeHtml(row.count)}</td>` +
                  `<td>${escapeHtml(formatMetricMoney(row.value, row.currency))}</td></tr>`
                ).join("") + '</tbody></table>';
            }

            function dueDateTable(rows) {
              if (!rows.length) return '<div class="empty-state">None.</div>';
              return '<table class="section-table"><thead><tr>' +
                '<th>IRJ</th><th>Company</th><th>Supplier</th><th>Due</th><th>Value</th>' +
                '</tr></thead><tbody>' +
                rows.map(row => `<tr><td>${escapeHtml(row.irj_number || "—")}</td>` +
                  `<td>${escapeHtml(row.company)}</td><td>${escapeHtml(row.supplier)}</td>` +
                  `<td>${escapeHtml(row.due_date)}</td>` +
                  `<td>${escapeHtml(row.value == null ? "—" : formatMetricMoney(row.value, row.currency))}</td></tr>`
                ).join("") + '</tbody></table>';
            }

            async function loadMetrics() {
              const dashboard = document.getElementById("metrics-dashboard");
              const granularity = document.getElementById("metrics-granularity").value;
              dashboard.className = "empty-state";
              dashboard.textContent = "Loading metrics…";
              try {
                const response = await fetch(
                  `/api/admin/metrics?granularity=${encodeURIComponent(granularity)}`
                );
                if (!response.ok) {
                  const body = await response.json().catch(() => ({}));
                  throw new Error(body.detail || "Metrics could not be loaded.");
                }
                const metrics = await response.json();
                const maximum = Math.max(1, ...metrics.volume.map(row => row.count));
                const totalVolume = metrics.volume.reduce((sum, row) => sum + row.count, 0);
                dashboard.className = "metrics-grid";
                dashboard.innerHTML = `
                  <section class="metric-panel wide">
                    <h4>Invoice volume <span class="metric-summary">${totalVolume}</span></h4>
                    <div class="metric-bars">
                      ${metrics.volume.map(row => `
                        <div class="metric-bar-row">
                          <span>${escapeHtml(row.period)}</span>
                          <div class="metric-bar-track"><div class="metric-bar-fill"
                            style="width: ${Math.round((row.count / maximum) * 100)}%"></div></div>
                          <strong>${escapeHtml(row.count)}</strong>
                        </div>
                      `).join("")}
                    </div>
                  </section>
                  <section class="metric-panel">
                    <h4>Overdue invoices (${metrics.due_dates.overdue.length})</h4>
                    ${dueDateTable(metrics.due_dates.overdue)}
                  </section>
                  <section class="metric-panel">
                    <h4>Due within 7 days (${metrics.due_dates.approaching.length})</h4>
                    ${dueDateTable(metrics.due_dates.approaching)}
                  </section>
                  <section class="metric-panel wide">
                    <h4>Total pending invoice value by company</h4>
                    ${metricValueTable(metrics.pending_by_company, "Company")}
                  </section>
                  <section class="metric-panel wide">
                    <h4>Spend by supplier and company</h4>
                    ${metricValueTable(metrics.spend_by_supplier_company, "Company — Supplier")}
                  </section>
                  ${metrics.due_dates.unavailable_count ? `
                    <p class="admin-help">${metrics.due_dates.unavailable_count} pending invoice(s)
                    have no calculable due date because the invoice date or deterministic payment
                    terms are unavailable.</p>
                  ` : ""}
                `;
              } catch (error) {
                dashboard.className = "empty-state";
                dashboard.textContent = error.message;
              }
            }

            function operationsQueueTable(items, status) {
              if (!items.length) return '<div class="empty-state">None.</div>';
              return '<table class="section-table"><thead><tr>' +
                '<th>Received</th><th>Message</th><th>Attempts</th><th>Error</th><th></th>' +
                '</tr></thead><tbody>' + items.map(item => `<tr>` +
                  `<td>${escapeHtml(formatUkTimestamp(item.received_at))}</td>` +
                  `<td title="${escapeHtml(item.message_id)}">${escapeHtml(item.message_id)}</td>` +
                  `<td>${escapeHtml(item.attempts)}</td>` +
                  `<td>${escapeHtml(item.last_error || "—")}</td>` +
                  `<td>${status === "failed" ? `<button type="button" class="secondary" ` +
                    `data-retry-notification="${item.id}">Retry</button>` : ""}</td>` +
                  `</tr>`).join("") + '</tbody></table>';
            }

            async function loadOperations() {
              const dashboard = document.getElementById("operations-dashboard");
              dashboard.className = "empty-state";
              dashboard.textContent = "Loading worker and queue status…";
              try {
                const operations = await fetchAdminJson("/api/admin/operations");
                const worker = operations.worker;
                const workerStatus = worker
                  ? `${worker.status} · last seen ${formatUkTimestamp(worker.last_seen_at)} ` +
                    `(${worker.age_seconds}s ago)`
                  : "No heartbeat recorded";
                dashboard.className = "metrics-grid";
                dashboard.innerHTML = `
                  <section class="metric-panel wide">
                    <h4>Outlook invoice worker</h4>
                    <p><strong>${escapeHtml(workerStatus)}</strong></p>
                    ${worker && worker.last_success_at
                      ? `<p>Last successful cycle: ${escapeHtml(formatUkTimestamp(worker.last_success_at))}</p>`
                      : ""}
                    ${worker && worker.last_error
                      ? `<p class="admin-help">Last error: ${escapeHtml(worker.last_error)}</p>`
                      : ""}
                    ${operations.alerts.length
                      ? `<ul>${operations.alerts.map(alert => `<li>${escapeHtml(alert)}</li>`).join("")}</ul>`
                      : '<p>No active monitoring alerts.</p>'}
                  </section>
                  <section class="metric-panel wide">
                    <h4>Failed queue items (${operations.queue.failed.length})</h4>
                    ${operationsQueueTable(operations.queue.failed, "failed")}
                  </section>
                  <section class="metric-panel">
                    <h4>Pending (${operations.queue.pending.length})</h4>
                    ${operationsQueueTable(operations.queue.pending, "pending")}
                  </section>
                  <section class="metric-panel">
                    <h4>Processing (${operations.queue.processing.length})</h4>
                    ${operationsQueueTable(operations.queue.processing, "processing")}
                  </section>`;
                dashboard.querySelectorAll("[data-retry-notification]").forEach(button => {
                  button.addEventListener("click", async () => {
                    button.disabled = true;
                    try {
                      await postJson(
                        `/api/admin/outlook-queue/${button.dataset.retryNotification}/retry`,
                        {}
                      );
                      showToast("Queue item returned to pending.");
                      await loadOperations();
                    } catch (error) {
                      button.disabled = false;
                      showToast(error.message, true);
                    }
                  });
                });
              } catch (error) {
                dashboard.className = "empty-state";
                dashboard.textContent = error.message;
              }
            }

            function renderAllSections() {
              renderSectionTable(
                "needs-review-table",
                SECTION_STATUSES["needs-review"],
                [
                  ...BASE_COLUMNS,
                  { label: "Return stage", value: i => i.review_return_status || "Requires a dedicated resolution" },
                ],
                invoice => `
                  ${pdfLinkButton(invoice)}
                  ${invoice.document_type === "statement" ? `
                    <button data-action="mark-invoice" data-id="${invoice.id}" class="secondary">Treat as invoice</button>
                  ` : `
                    <button data-action="open-review" data-id="${invoice.id}" class="secondary">Open invoice review</button>
                  `}
                  <select data-statement-company="${invoice.id}" aria-label="Statement company"></select>
                  <button data-action="file-statement" data-id="${invoice.id}">File as statement</button>
                  ${invoice.review_return_status ? `
                    <button data-action="accept-review" data-id="${invoice.id}">Accept</button>
                  ` : ""}
                  ${invoice.document_type === "invoice" ? `
                    <button data-action="reject-flagged" data-id="${invoice.id}" class="danger">Reject</button>
                  ` : ""}
                `,
                isFlaggedInvoice
              );
              renderSectionTable(
                "po-matching-table",
                SECTION_STATUSES["po-matching"],
                [
                  ...BASE_COLUMNS,
                  { label: "PO number", value: i => i.po_number || "—" },
                ],
                invoice => `
                  ${pdfLinkButton(invoice)}
                  ${currentUser && currentUser.role === "purchasing" ? "" : `
                    <button data-action="po-match" data-id="${invoice.id}">Mark matched</button>
                    <button data-action="po-query" data-id="${invoice.id}">Record query</button>
                    <button data-action="po-reject" data-id="${invoice.id}" class="danger">Reject</button>
                  `}
                `
              );
              renderSectionTable(
                "sage-registration-table",
                SECTION_STATUSES["sage-registration"],
                BASE_COLUMNS,
                invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="register-sage" data-id="${invoice.id}" data-irj="${escapeHtml(invoice.irj_number || "")}">Confirm Sage registration / IRJ</button>
                `
              );
              renderSectionTable(
                "approver1-table",
                SECTION_STATUSES["approver1"],
                BASE_COLUMNS,
                invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="approve" data-level="1" data-id="${invoice.id}">Approve</button>
                  ${invoice.hold_level === 1 ? "" : `
                    <button data-action="hold" data-level="1" data-id="${invoice.id}" class="secondary">Record query</button>
                  `}
                  <button data-action="reject" data-level="1" data-id="${invoice.id}" class="danger">Reject</button>
                `
              );
              renderSectionTable(
                "approver2-table",
                SECTION_STATUSES["approver2"],
                BASE_COLUMNS,
                invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="approve" data-level="2" data-id="${invoice.id}">Approve</button>
                  ${invoice.hold_level === 2 ? "" : `
                    <button data-action="hold" data-level="2" data-id="${invoice.id}" class="secondary">Record query</button>
                  `}
                  <button data-action="reject" data-level="2" data-id="${invoice.id}" class="danger">Reject</button>
                `
              );
              renderSectionTable(
                "on-hold-table",
                SECTION_STATUSES["on-hold"],
                BASE_COLUMNS,
                invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="resume" data-id="${invoice.id}">Resume approval</button>
                `,
                invoice => invoice.status === "Approval Query / On Hold" || Boolean(invoice.hold_level)
              );
              renderSectionTable(
                "approved-table",
                SECTION_STATUSES["approved"],
                BASE_COLUMNS,
                invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="select-payment-route" data-route="bacs" data-id="${invoice.id}">BACS</button>
                  <button data-action="select-payment-route" data-route="bankline" data-id="${invoice.id}">Bankline</button>
                  <button data-action="select-payment-route" data-route="foreign_poa" data-id="${invoice.id}">Foreign POA</button>
                `
              );
              for (const [containerId, section] of [
                ["payment-bacs-table", "payment-bacs"],
                ["payment-bankline-table", "payment-bankline"],
                ["payment-foreign-poa-table", "payment-foreign-poa"],
              ]) {
                renderSectionTable(
                  containerId,
                  SECTION_STATUSES[section],
                  [
                    ...BASE_COLUMNS,
                    { label: "Payment route", value: i => i.payment_method || "—" },
                  ],
                  invoice => `
                    ${pdfLinkButton(invoice)}
                    <button data-action="pay" data-id="${invoice.id}">Record payment</button>
                  `
                );
              }
              renderSectionTable(
                "reconciliation-table",
                SECTION_STATUSES["reconciliation"],
                [
                  ...BASE_COLUMNS,
                  { label: "Payment date", value: i => i.payment_date || "—" },
                  { label: "Paid by", value: i => i.paid_by || "—" },
                ],
                invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="reconcile" data-id="${invoice.id}">Mark reconciled</button>
                `
              );
              renderSectionTable(
                "complete-table",
                SECTION_STATUSES["complete"],
                [
                  ...BASE_COLUMNS,
                  { label: "Marked reconciled", value: i => i.reconciled_at ? i.reconciled_at.slice(0, 10) : "—" },
                  { label: "Appeared on bank statement", value: i => i.reconciliation_date || "—" },
                  { label: "Completed by", value: i => i.reconciled_by || i.foreign_allocated_by || "—" },
                ],
                invoice => pdfLinkButton(invoice)
              );
              renderSectionTable(
                "rejected-table",
                SECTION_STATUSES["rejected"],
                BASE_COLUMNS,
                invoice => pdfLinkButton(invoice)
              );
            }

            async function readResponseBody(response) {
              const text = await response.text();
              if (!text) return {};
              try {
                return JSON.parse(text);
              } catch {
                return { detail: text };
              }
            }

            async function fetchAdminJson(url) {
              const response = await fetch(url);
              const body = await readResponseBody(response);
              if (!response.ok) {
                const detail = body.detail;
                const message = typeof detail === "string" && detail !== "Internal Server Error"
                  ? `: ${detail}` : "";
                throw new Error(`${url} failed (HTTP ${response.status})${message}`);
              }
              return body;
            }

            async function sendJson(url, method, body) {
              const response = await fetch(url, {
                method,
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body || {}),
              });
              const detail = await readResponseBody(response);
              if (!response.ok) {
                throw new Error(detail.detail || `Request failed (${response.status}).`);
              }
              return detail;
            }

            async function postJson(url, body) {
              return sendJson(url, "POST", body);
            }

            async function putJson(url, body) {
              return sendJson(url, "PUT", body);
            }

            async function collectDomesticPayment(invoice) {
              const response = await fetch(
                `/api/supplier-terms?company=${encodeURIComponent(invoice.company || "")}` +
                `&supplier=${encodeURIComponent(invoice.supplier || "")}`
              );
              const terms = response.ok ? await response.json() : {};
              const dialog = document.getElementById("payment-dialog");
              const form = document.getElementById("payment-form");
              const method = document.getElementById("payment-method");
              const account = document.getElementById("payment-supplier-account");
              const profiles = terms.profiles || [];
              account.innerHTML = profiles.length
                ? profiles.map(profile =>
                    `<option value="${escapeHtml(profile.supplier_account_number || "")}">` +
                    `${escapeHtml(profile.supplier_account_number || "No account number")} — ` +
                    `${escapeHtml(profile.bank_account || "No bank specified")}` +
                    `</option>`
                  ).join("")
                : '<option value="">No configured supplier account</option>';
              account.disabled = !profiles.length;
              const applyProfile = () => {
                const profile = profiles.find(item => item.supplier_account_number === account.value) || profiles[0] || {};
                method.value = invoice.payment_method || "";
                document.getElementById("payment-terms").value = profile.payment_terms_notice || "";
                document.getElementById("payment-bank-account").value = profile.bank_account || "";
              };
              account.onchange = applyProfile;
              applyProfile();
              document.getElementById("payment-date").value = new Date().toISOString().slice(0, 10);
              document.getElementById("payment-reference").value = "";
              document.getElementById("payment-supplier-context").textContent =
                invoice.supplier || "Supplier";

              return new Promise(resolve => {
                let settled = false;
                const finish = value => {
                  if (settled) return;
                  settled = true;
                  resolve(value);
                };
                form.onsubmit = event => {
                  event.preventDefault();
                  const payment = {
                    payment_date: document.getElementById("payment-date").value,
                    supplier_account_number: account.value || null,
                    payment_reference: document.getElementById("payment-reference").value,
                    payment_method: method.value,
                  };
                  dialog.close();
                  finish(payment);
                };
                document.getElementById("payment-cancel").onclick = () => dialog.close();
                dialog.onclose = () => finish(null);
                dialog.showModal();
              });
            }

            async function collectSageRegistration(invoice) {
              const dialog = document.getElementById("sage-registration-dialog");
              const form = document.getElementById("sage-registration-form");
              const irjInput = document.getElementById("sage-registration-irj");
              const errorEl = document.getElementById("sage-registration-error");
              document.getElementById("sage-registration-context").textContent =
                `${invoice.supplier || "Supplier"} — ${invoice.original_filename}`;
              irjInput.value = invoice.irj_number || "";
              const irjSetting = irjConfigurations.get(invoice.company);
              const isManual = irjSetting ? irjSetting.mode === "manual" : false;
              irjInput.readOnly = !isManual && Boolean(invoice.irj_number);
              irjInput.title = isManual
                ? "Enter the IRJ from the paper records."
                : "This IRJ was allocated automatically.";
              errorEl.textContent = "";

              return new Promise(resolve => {
                let settled = false;
                const finish = value => {
                  if (settled) return;
                  settled = true;
                  resolve(value);
                };
                form.onsubmit = event => {
                  event.preventDefault();
                  const irjNumber = irjInput.value.trim();
                  if (!/^\\d{6}$/.test(irjNumber)) {
                    errorEl.textContent = "Enter exactly six digits.";
                    return;
                  }
                  dialog.close();
                  finish(irjNumber);
                };
                document.getElementById("sage-registration-cancel").onclick = () => dialog.close();
                dialog.onclose = () => finish(null);
                dialog.showModal();
                irjInput.focus();
              });
            }

            async function handleRowAction(button) {
              const action = button.dataset.action;
              const id = button.dataset.id;
              const originalText = button.textContent;
              button.disabled = true;
              button.setAttribute("aria-busy", "true");
              button.textContent = "Working…";
              try {
                if (action === "po-match") {
                  await postJson(`/api/invoices/${id}/po-match`, { matched: true, notes: null });
                } else if (action === "open-review") {
                  openedFlaggedInvoiceId = id;
                  document.querySelector('#section-nav button[data-tab="incoming"]').click();
                  await refreshPicker();
                  return;
                } else if (action === "file-statement") {
                  const company = button.closest("tr")
                    ?.querySelector("[data-statement-company]")?.value;
                  if (!company) {
                    throw new Error("Select the statement company first.");
                  }
                  await postJson(`/api/invoices/${id}/file-statement`, { company });
                  showToast(`Statement filed to Statements/${company}.`);
                } else if (action === "mark-invoice") {
                  await postJson(`/api/invoices/${id}/mark-as-invoice`, {});
                  showToast("Document returned to invoice review.");
                } else if (action === "accept-review") {
                  await postJson(`/api/invoices/${id}/review-decision`, {
                    accepted: true,
                    reason: null,
                  });
                } else if (action === "reject-flagged") {
                  const reason = window.prompt("Reason for rejecting this invoice:");
                  if (!reason) return;
                  await postJson(`/api/invoices/${id}/reject-flagged`, { reason });
                } else if (action === "po-query") {
                  const notes = window.prompt("Describe the PO matching issue:");
                  if (notes === null) return;
                  const queryCategory = window.prompt(
                    "Query category (e.g. price discrepancy, missing PO, quantity mismatch):"
                  );
                  const purchasingContact = window.prompt("Purchasing contact to route this query to (optional):");
                  await postJson(`/api/invoices/${id}/po-match`, {
                    matched: false,
                    notes,
                    query_category: queryCategory || null,
                    purchasing_contact: purchasingContact || null,
                  });
                } else if (action === "po-reject") {
                  const reason = window.prompt("Reason for rejecting this PO invoice:");
                  if (!reason) return;
                  await postJson(`/api/invoices/${id}/reject`, { reason });
                } else if (action === "register-sage") {
                  const invoice = invoices.find(item => String(item.id) === String(id));
                  if (!invoice) throw new Error("Invoice could not be found.");
                  const irjNumber = await collectSageRegistration(invoice);
                  if (!irjNumber) return;
                  await postJson(`/api/invoices/${id}/register-sage`, {
                    irj_number: irjNumber,
                  });
                } else if (action === "approve" || action === "reject") {
                  const comments = window.prompt(
                    action === "reject" ? "Reason for rejection:" : "Approval comments (optional):"
                  );
                  if (comments === null && action === "reject") return;
                  await postJson(`/api/invoices/${id}/approve`, {
                    level: Number(button.dataset.level),
                    decision: action === "approve" ? "approved" : "rejected",
                    comments: comments || null,
                  });
                } else if (action === "hold") {
                  const comments = window.prompt("Reason for placing this invoice on hold / query (required):");
                  if (!comments) return;
                  await postJson(`/api/invoices/${id}/approve`, {
                    level: Number(button.dataset.level),
                    decision: "on_hold",
                    comments,
                  });
                } else if (action === "resume") {
                  const notes = window.prompt("Resolution notes for resuming approval (optional):");
                  await postJson(`/api/invoices/${id}/resume-approval`, { resolution_notes: notes || null });
                } else if (action === "select-payment-route") {
                  await postJson(`/api/invoices/${id}/payment-route`, {
                    route: button.dataset.route,
                  });
                } else if (action === "pay") {
                  const invoice = invoices.find(item => String(item.id) === String(id));
                  if (!invoice) throw new Error("Invoice could not be found.");
                  const payment = await collectDomesticPayment(invoice);
                  if (!payment) return;
                  await postJson(`/api/invoices/${id}/pay`, payment);
                } else if (action === "reconcile") {
                  const reconciliationDate = window.prompt("Date shown on bank statement (YYYY-MM-DD):", new Date().toISOString().slice(0, 10));
                  if (!reconciliationDate) return;
                  const notes = window.prompt("Reconciliation notes (optional):");
                  await postJson(`/api/invoices/${id}/reconcile`, {
                    reconciliation_date: reconciliationDate,
                    notes: notes || null,
                  });
                }
                await loadInvoices();
              } catch (error) {
                showToast(error.message, true);
              } finally {
                if (button.isConnected) {
                  button.disabled = false;
                  button.removeAttribute("aria-busy");
                  button.textContent = originalText;
                }
              }
            }

            document.getElementById("flag-review-button").addEventListener("click", async () => {
              const invoiceId = picker.value;
              if (!invoiceId) return;
              const reason = window.prompt("Reason for flagging this invoice for review:");
              if (!reason) return;
              try {
                await postJson(`/api/invoices/${invoiceId}/flag-review`, { reason });
                await loadInvoices();
              } catch (error) {
                showToast(error.message, true);
              }
            });

            document.getElementById("delete-invoice-button").addEventListener("click", async () => {
              const invoice = invoices.find(item => String(item.id) === picker.value);
              if (!invoice) return;
              if (!window.confirm(
                `Permanently delete "${invoice.original_filename}" from the application ` +
                "and SharePoint? This cannot be undone."
              )) return;
              try {
                await sendJson(`/api/invoices/${invoice.id}`, "DELETE");
                displayedInvoiceId = null;
                pdfFrame.removeAttribute("src");
                pdfFrame.style.display = "none";
                pdfEmpty.style.display = "grid";
                showToast("Invoice permanently deleted.");
                await loadInvoices();
              } catch (error) {
                showToast(error.message, true);
              }
            });

            document.getElementById("confirm-invoice-button").addEventListener("click", async () => {
              await submitConfirm(false);
            });

            document.getElementById("override-duplicate-button").addEventListener("click", async () => {
              await submitConfirm(true);
            });

            document.getElementById("cancel-duplicate-button").addEventListener("click", async () => {
              const invoiceId = picker.value;
              if (!invoiceId || !window.confirm("Confirm this invoice is a duplicate and cancel it?")) return;
              try {
                await postJson(`/api/invoices/${invoiceId}/cancel-duplicate`, {});
                showToast("Duplicate invoice cancelled.");
                await loadInvoices();
              } catch (error) {
                showToast(error.message, true);
              }
            });

            document.getElementById("retry-approval-route-button").addEventListener("click", async () => {
              const invoice = invoices.find(item => String(item.id) === picker.value);
              if (!invoice || !invoice.irj_number) return;
              try {
                await postJson(`/api/invoices/${invoice.id}/register-sage`, {
                  irj_number: invoice.irj_number,
                });
                showToast("Approver route checked again.");
                await loadInvoices();
              } catch (error) {
                showToast(error.message, true);
              }
            });

            async function submitConfirm(overrideDuplicate) {
              const invoiceId = displayedInvoiceId;
              if (!invoiceId) return;
              const company = document.getElementById("preview-company").value;
              const supplier = document.getElementById("preview-supplier").value;
              if (!company || !supplier) {
                showToast("Company and supplier are required.", true);
                return;
              }
              const body = {
                company,
                supplier,
                supplier_invoice_number: document.getElementById("preview-supplier-invoice").value || null,
                purchase_order_number: document.getElementById("preview-po").value || null,
                invoice_date: document.getElementById("preview-invoice-date").value || null,
                invoice_value: document.getElementById("preview-value").value
                  ? Number(document.getElementById("preview-value").value)
                  : null,
                currency: document.getElementById("preview-currency").value || "GBP",
                override_duplicate: overrideDuplicate,
                correction_reason: document.getElementById("correction-reason").value || null,
              };
              try {
                await postJson(`/api/invoices/${invoiceId}/confirm`, body);
                openedFlaggedInvoiceId = null;
                showToast("Invoice routed successfully.");
                await loadInvoices();
              } catch (error) {
                showToast(error.message, true);
              }
            }

            document.getElementById("manual-upload-form").addEventListener("submit", async event => {
              event.preventDefault();
              const statusEl = document.getElementById("manual-upload-status");
              const fileInput = document.getElementById("manual-upload-file");
              const file = fileInput.files[0];
              statusEl.className = "";
              statusEl.textContent = "";
              if (!file) {
                statusEl.className = "error";
                statusEl.textContent = "Choose a PDF file first.";
                return;
              }
              const formData = new FormData();
              formData.append("file", file);
              formData.append("sender_name", document.getElementById("manual-upload-sender-name").value);
              formData.append("sender_address", document.getElementById("manual-upload-sender-address").value);
              formData.append("subject", document.getElementById("manual-upload-subject").value);
              const submitButton = document.getElementById("manual-upload-submit");
              submitButton.disabled = true;
              try {
                const response = await fetch("/api/invoices/manual-upload", { method: "POST", body: formData });
                if (!response.ok) {
                  const detail = await response.json().catch(() => ({}));
                  throw new Error(detail.detail || `Upload failed (${response.status}).`);
                }
                const record = await response.json();
                const destination = record.status === "Needs Review"
                  ? "Flagged"
                  : record.status === "Awaiting AI Extraction"
                    ? "Incoming Invoices"
                    : record.status;
                statusEl.className = "success";
                statusEl.textContent = `Uploaded '${record.original_filename}' to ${destination}.`;
                event.target.reset();
                showToast(`Invoice manually added to ${destination}.`);
                if (record.status === "Needs Review") {
                  openedFlaggedInvoiceId = record.id;
                }
                await loadInvoices();
                if (record.status === "Needs Review") {
                  activateTab("needs-review");
                }
              } catch (error) {
                statusEl.className = "error";
                statusEl.textContent = error.message;
              } finally {
                submitButton.disabled = false;
              }
            });

            document.getElementById("section-nav").addEventListener("click", event => {
              const button = event.target.closest("button[data-tab]");
              if (!button) return;
              storeValue(`${SCROLL_STORAGE_PREFIX}${currentTab}`, String(window.scrollY));
              activateTab(button.dataset.tab);
            });

            function applyRoleVisibility(role) {
              const allowedTabs = ROLE_TABS[role] || Object.keys(SECTION_STATUSES);
              document.querySelectorAll("#section-nav button[data-tab]").forEach(button => {
                const isAllowed = allowedTabs.includes(button.dataset.tab);
                button.style.display = isAllowed ? "" : "none";
              });
              const desiredTab = allowedTabs.includes(currentTab)
                ? currentTab
                : allowedTabs[0];
              activateTab(desiredTab);
            }

            window.addEventListener("beforeunload", () => {
              storeValue(`${SCROLL_STORAGE_PREFIX}${currentTab}`, String(window.scrollY));
            });

            async function pollActivity() {
              try {
                const url = lastActivityId === null
                  ? "/api/activity"
                  : `/api/activity?since_id=${lastActivityId}`;
                const response = await fetch(url);
                if (!response.ok) return;
                const events = await response.json();
                if (!events.length) return;
                const currentRole = currentUser ? currentUser.role : null;
                let sawInvoiceEvent = false;
                for (const event of events) {
                  lastActivityId = event.id;
                  if (event.target_role === currentRole || event.target_role === "all") {
                    showToast(event.message, event.event_type.includes("error") || event.event_type === "rejected");
                  }
                  if (event.invoice_id) sawInvoiceEvent = true;
                }
                if (sawInvoiceEvent) await loadInvoices();
              } catch (error) {
                // Network errors are ignored; polling retries on the next tick.
              }
            }

            async function initActivityCursor() {
              const response = await fetch("/api/activity");
              if (!response.ok) { lastActivityId = 0; return; }
              const events = await response.json();
              lastActivityId = events.length ? events[events.length - 1].id : 0;
            }

            function connectLiveUpdates() {
              if (activityEventSource !== null) activityEventSource.close();
              activityEventSource = new EventSource(
                `/api/activity/stream?since_id=${lastActivityId || 0}`
              );
              activityEventSource.addEventListener("invoice-update", async () => {
                await pollActivity();
                if (
                  currentUser?.role === "admin" &&
                  document.getElementById("admin-bi-metrics").open
                ) {
                  if (metricsRefreshTimer !== null) {
                    window.clearTimeout(metricsRefreshTimer);
                  }
                  metricsRefreshTimer = window.setTimeout(loadMetrics, 3000);
                }
              });
            }

            // ---------------------------------------------------------
            // Authentication + admin config panel
            // ---------------------------------------------------------

            function splitCommaList(value) {
              return (value || "")
                .split(",")
                .map(part => part.trim())
                .filter(Boolean);
            }

            function renderSimpleTable(
              container,
              columns,
              rows,
              onDelete,
              idKey = "id",
              onEdit = null
            ) {
              if (!rows.length) {
                container.innerHTML = '<p class="empty">Nothing here yet.</p>';
                return;
              }
              const hasActions = Boolean(onDelete || onEdit);
              const head = columns.map(c => `<th>${escapeHtml(c.label)}</th>`).join("") +
                (hasActions ? "<th>Actions</th>" : "");
              const body = rows
                .map((row, index) => {
                  const cells = columns
                    .map(c => `<td>${escapeHtml(c.value(row) ?? "—")}</td>`)
                    .join("");
                  const actionCell = hasActions
                    ? `<td><div class="row-actions">` +
                      (onEdit
                        ? `<button class="secondary" data-edit-index="${index}">Edit</button>`
                        : "") +
                      (onDelete
                        ? `<button class="danger" data-delete-index="${index}">Delete</button>`
                        : "") +
                      `</div></td>`
                    : "";
                  return `<tr>${cells}${actionCell}</tr>`;
                })
                .join("");
              container.innerHTML =
                `<div class="admin-table-wrap"><table class="admin-table">` +
                `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>` +
                `</table></div>`;
              if (onDelete) {
                container.querySelectorAll("[data-delete-index]").forEach(button => {
                  button.addEventListener("click", () => {
                    const row = rows[Number(button.dataset.deleteIndex)];
                    onDelete(row[idKey]);
                  });
                });
              }
              if (onEdit) {
                container.querySelectorAll("[data-edit-index]").forEach(button => {
                  button.addEventListener(
                    "click",
                    () => onEdit(rows[Number(button.dataset.editIndex)])
                  );
                });
              }
            }

            function companyGroupLabel(company) {
              if (company === "*") return "All invoice companies";
              return company || "Unassigned suppliers";
            }

            function renderCompanyGroupedTables(
              container,
              groups,
              columns,
              onDelete,
              idKey = "id",
              onEdit = null
            ) {
              container.replaceChildren();
              container.classList.add("admin-company-groups");
              const entries = [...groups.entries()].sort(([left], [right]) =>
                companyGroupLabel(left).localeCompare(companyGroupLabel(right))
              );
              if (!entries.length) {
                container.innerHTML = '<p class="empty">Nothing here yet.</p>';
                return;
              }
              for (const [company, rows] of entries) {
                const details = document.createElement("details");
                details.className = "admin-company-group";
                const summary = document.createElement("summary");
                summary.textContent =
                  `${companyGroupLabel(company)} (${rows.length})`;
                const content = document.createElement("div");
                content.className = "admin-company-group-content";
                details.append(summary, content);
                container.appendChild(details);
                renderSimpleTable(
                  content,
                  columns,
                  rows,
                  onDelete,
                  idKey,
                  onEdit
                );
              }
            }

            function rowsGroupedByCompany(rows) {
              const groups = new Map();
              for (const row of rows) {
                const company = row.company || "";
                if (!groups.has(company)) groups.set(company, []);
                groups.get(company).push(row);
              }
              return groups;
            }

            function suppliersGroupedByCompany(suppliers, matrix, supplierTerms) {
              const associations = new Map(
                suppliers.map(supplier => [supplier.name, new Set()])
              );
              for (const supplier of suppliers) {
                if (supplier.default_company) {
                  associations.get(supplier.name).add(supplier.default_company);
                }
              }
              for (const record of [...matrix, ...supplierTerms]) {
                if (associations.has(record.supplier)) {
                  associations.get(record.supplier).add(record.company || "");
                }
              }
              const groups = new Map();
              for (const supplier of suppliers) {
                const companies = associations.get(supplier.name);
                if (!companies.size) companies.add("");
                for (const company of companies) {
                  if (!groups.has(company)) groups.set(company, []);
                  groups.get(company).push(supplier);
                }
              }
              return groups;
            }

            function openAdminEditor(title, fields, onSave) {
              const dialog = document.getElementById("admin-edit-dialog");
              const form = document.getElementById("admin-edit-form");
              const container = document.getElementById("admin-edit-fields");
              document.getElementById("admin-edit-title").textContent = title;
              container.replaceChildren();
              const controls = new Map();
              for (const field of fields) {
                const label = document.createElement("label");
                label.textContent = field.label;
                if (field.fullWidth) label.classList.add("full-width");
                let control;
                if (field.type === "select") {
                  control = document.createElement("select");
                  for (const [value, optionLabel] of field.options || []) {
                    const option = document.createElement("option");
                    option.value = value;
                    option.textContent = optionLabel;
                    control.appendChild(option);
                  }
                } else if (field.type === "textarea") {
                  control = document.createElement("textarea");
                } else {
                  control = document.createElement("input");
                  control.type = field.type || "text";
                }
                control.value = field.value ?? "";
                control.required = Boolean(field.required);
                control.autocomplete = field.type === "password" ? "new-password" : "off";
                label.appendChild(control);
                container.appendChild(label);
                controls.set(field.key, { control, field });
              }
              document.getElementById("admin-edit-cancel").onclick = () => dialog.close();
              form.onsubmit = async event => {
                event.preventDefault();
                const values = {};
                for (const [key, { control, field }] of controls) {
                  if (field.omitWhenBlank && !control.value) continue;
                  values[key] = field.nullWhenBlank && !control.value
                    ? null
                    : control.value;
                }
                try {
                  await onSave(values);
                  dialog.close();
                } catch (error) {
                  showToast(error.message, true);
                }
              };
              dialog.showModal();
            }

            function companyOptions(includeAll = false) {
              return [
                ["", "Select invoice company…"],
                ...(includeAll ? [["*", "All invoice companies"]] : []),
                ...adminCompanies.map(company => [company.name, company.name]),
              ];
            }

            function supplierOptions() {
              return [
                ["", "Select supplier company…"],
                ...adminSuppliers.map(supplier => [supplier.name, supplier.name]),
              ];
            }

            function includeCurrentOption(options, value, label = value) {
              if (!value || options.some(([optionValue]) => optionValue === value)) {
                return options;
              }
              return [...options, [value, label]];
            }

            function replaceSelectOptions(select, options) {
              const selected = select.value;
              select.replaceChildren();
              for (const [value, label] of options) {
                const option = document.createElement("option");
                option.value = value;
                option.textContent = label;
                select.appendChild(option);
              }
              if (options.some(([value]) => value === selected)) {
                select.value = selected;
              }
            }

            function supplierBelongsToCompany(
              supplier,
              company,
              matrix = adminMatrix,
              supplierTerms = adminSupplierTerms
            ) {
              if (!company || company === "*") return true;
              const supplierName = supplier.name.trim().toLocaleLowerCase();
              const companyName = company.trim().toLocaleLowerCase();
              if (
                supplier.default_company?.trim().toLocaleLowerCase() === companyName
              ) return true;
              return [...matrix, ...supplierTerms].some(record =>
                record.supplier.trim().toLocaleLowerCase() === supplierName &&
                (
                  record.company === "*" ||
                  record.company.trim().toLocaleLowerCase() === companyName
                )
              );
            }

            function refreshAdminMatrixSupplierOptions(selectedSupplier = "") {
              const company = document.getElementById("admin-matrix-company").value;
              const suppliers = adminSuppliers.filter(supplier =>
                supplierBelongsToCompany(supplier, company)
              );
              replaceSelectOptions(
                document.getElementById("admin-matrix-supplier"),
                [
                  [
                    "",
                    company
                      ? "Select supplier company…"
                      : "Select an invoice company first…",
                  ],
                  ...suppliers.map(supplier => [supplier.name, supplier.name]),
                ]
              );
              const select = document.getElementById("admin-matrix-supplier");
              select.disabled = !company;
              if (
                selectedSupplier &&
                suppliers.some(supplier => supplier.name === selectedSupplier)
              ) {
                select.value = selectedSupplier;
              }
            }

            function loadAdminReferenceOptions(companies, suppliers) {
              const companyOptions = companies.map(company => [
                company.name,
                company.name,
              ]);
              replaceSelectOptions(
                document.getElementById("admin-supplier-default-company"),
                [["", "No default company"], ...companyOptions]
              );
              replaceSelectOptions(
                document.getElementById("admin-import-company"),
                [["", "Select invoice company…"], ...companyOptions]
              );
              replaceSelectOptions(
                document.getElementById("admin-matrix-company"),
                [
                  ["", "Select invoice company…"],
                  ["*", "All invoice companies"],
                  ...companyOptions,
                ]
              );
              refreshAdminMatrixSupplierOptions();
            }

            async function loadSharePointFolderOptions() {
              const status = document.getElementById("admin-sharepoint-folder-status");
              const rootSelect = document.getElementById("admin-company-root-folder");
              const preview = document.getElementById("admin-company-folder-preview");
              const submit = document.getElementById("admin-company-submit");
              try {
                const response = await fetch("/api/admin/sharepoint/folders");
                const body = await response.json();
                if (!response.ok) {
                  throw new Error(body.detail || "SharePoint folders could not be loaded.");
                }
                sharePointCompanyFolders = new Map(
                  body.company_roots.map(structure => [structure.root, structure])
                );
                rootSelect.replaceChildren();
                const emptyOption = document.createElement("option");
                emptyOption.value = "";
                emptyOption.textContent = "Select SharePoint company folder…";
                rootSelect.appendChild(emptyOption);
                for (const structure of body.company_roots) {
                  const option = document.createElement("option");
                  option.value = structure.root;
                  option.textContent = structure.root.split("/").pop();
                  rootSelect.appendChild(option);
                }
                rootSelect.disabled = false;
                submit.disabled = false;
                status.textContent =
                  `${body.company_roots.length} complete company folder structure(s) found. ` +
                  `Incoming and Rejected are shared across all companies.`;
                preview.textContent =
                  "Select a company folder to preview its workflow destinations.";
              } catch (error) {
                sharePointCompanyFolders = new Map();
                rootSelect.disabled = true;
                submit.disabled = true;
                status.textContent = `SharePoint folders unavailable: ${error.message}`;
              }
            }

            async function loadAdminPanel() {
              try {
                const [companies, suppliers, matrix, supplierTerms, threshold, irjSettings] = await Promise.all([
                  fetchAdminJson("/api/admin/companies"),
                  fetchAdminJson("/api/admin/suppliers"),
                  fetchAdminJson("/api/admin/approval-matrix"),
                  fetchAdminJson("/api/admin/supplier-terms"),
                  fetchAdminJson("/api/admin/ai-threshold"),
                  fetchAdminJson("/api/irj-configurations"),
                ]);
                adminCompanies = companies;
                adminSuppliers = suppliers;
                adminMatrix = matrix;
                adminSupplierTerms = supplierTerms;
                loadAdminReferenceOptions(companies, suppliers);
                const matrixQuery = document.getElementById("admin-matrix-filter")
                  .value.trim().toLowerCase();
                const matrixEmailFilter = document.getElementById("admin-matrix-email-filter").value;
                const visibleMatrix = matrix.filter(entry => {
                  const searchText = [entry.company, entry.supplier, entry.approver1_name,
                    entry.approver1_email, entry.approver2_name, entry.approver2_email]
                    .filter(Boolean).join(" ").toLowerCase();
                  const missingEmail = !entry.approver1_email ||
                    (entry.approver2_name && !entry.approver2_email);
                  return (!matrixQuery || searchText.includes(matrixQuery)) &&
                    (matrixEmailFilter !== "missing" || missingEmail);
                });
                renderSimpleTable(
                  document.getElementById("admin-companies-table"),
                  [
                    { label: "Name", value: c => c.name },
                    { label: "SharePoint company folder", value: c => c.sharepoint_root_folder },
                    {
                      label: "Workflow destinations",
                      value: c => `<div class="path-list">${[
                          ["Nominal", c.folder_structure.nominal_invoices],
                          ["PO Match", c.folder_structure.po_match],
                          ["Approved", c.folder_structure.approved_for_payment],
                          ["Paid", c.folder_structure.paid],
                          ["Reconciled", c.folder_structure.reconciled],
                        ].map(([label, path]) =>
                          `<div><strong>${escapeHtml(label)}</strong>` +
                          `<span class="path-value">${escapeHtml(path)}</span></div>`
                        ).join("")}</div>`
                    },
                    { label: "Aliases", value: c => (c.aliases || []).join(", ") },
                  ],
                  companies,
                  async name => {
                    await fetch(`/api/admin/companies/${encodeURIComponent(name)}`, { method: "DELETE" });
                    await loadAdminPanel();
                    await loadCompanies();
                    const irjResponse = await fetch("/api/irj-configurations");
                    if (irjResponse.ok) {
                      const settings = await irjResponse.json();
                      irjConfigurations = new Map(
                        settings.map(setting => [setting.company, setting])
                      );
                    }
                  },
                  "name",
                  company => {
                    const rootOptions = includeCurrentOption(
                      [
                        ["", "Select SharePoint company folder…"],
                        ...[...sharePointCompanyFolders.values()].map(structure => [
                          structure.root,
                          structure.root.split("/").pop(),
                        ]),
                      ],
                      company.sharepoint_root_folder
                    );
                    openAdminEditor(`Edit ${company.name}`, [
                      {
                        key: "sharepoint_root_folder",
                        label: "SharePoint company folder",
                        type: "select",
                        options: rootOptions,
                        value: company.sharepoint_root_folder,
                        required: true,
                        fullWidth: true,
                      },
                      {
                        key: "aliases",
                        label: "Aliases (comma separated)",
                        value: (company.aliases || []).join(", "),
                        fullWidth: true,
                      },
                      {
                        key: "vat_number",
                        label: "VAT number",
                        value: company.vat_number,
                        nullWhenBlank: true,
                      },
                      {
                        key: "address",
                        label: "Address",
                        type: "textarea",
                        value: company.address,
                        nullWhenBlank: true,
                        fullWidth: true,
                      },
                    ], async values => {
                      values.aliases = splitCommaList(values.aliases);
                      await putJson(
                        `/api/admin/companies/${encodeURIComponent(company.name)}`,
                        values
                      );
                      await loadAdminPanel();
                      await loadCompanies();
                    });
                  }
                );
                renderCompanyGroupedTables(
                  document.getElementById("admin-suppliers-table"),
                  suppliersGroupedByCompany(suppliers, matrix, supplierTerms),
                  [
                    { label: "Name", value: s => s.name },
                    { label: "Aliases", value: s => (s.aliases || []).join(", ") },
                    { label: "Default company", value: s => s.default_company },
                    { label: "Contact", value: s => s.contact_email },
                    { label: "Invoice no. pattern", value: s => s.invoice_number_pattern },
                  ],
                  async name => {
                    await fetch(`/api/admin/suppliers/${encodeURIComponent(name)}`, { method: "DELETE" });
                    await loadAdminPanel();
                    await loadSuppliers();
                  },
                  "name",
                  supplier => {
                    openAdminEditor(`Edit ${supplier.name}`, [
                      {
                        key: "aliases",
                        label: "Aliases (comma separated)",
                        value: (supplier.aliases || []).join(", "),
                        fullWidth: true,
                      },
                      {
                        key: "default_company",
                        label: "Default company",
                        type: "select",
                        options: includeCurrentOption(
                          [["", "No default company"], ...companyOptions().slice(1)],
                          supplier.default_company
                        ),
                        value: supplier.default_company,
                        nullWhenBlank: true,
                      },
                      {
                        key: "contact_email",
                        label: "Contact email",
                        type: "email",
                        value: supplier.contact_email,
                        nullWhenBlank: true,
                      },
                      {
                        key: "invoice_number_pattern",
                        label: "Invoice number pattern (# digit, @ letter, * alphanumeric)",
                        value: supplier.invoice_number_pattern,
                        nullWhenBlank: true,
                        fullWidth: true,
                      },
                    ], async values => {
                      values.aliases = splitCommaList(values.aliases);
                      await putJson(
                        `/api/admin/suppliers/${encodeURIComponent(supplier.name)}`,
                        values
                      );
                      await loadAdminPanel();
                      await loadSuppliers();
                    });
                  }
                );
                renderCompanyGroupedTables(
                  document.getElementById("admin-supplier-terms-table"),
                  rowsGroupedByCompany(supplierTerms),
                  [
                    { label: "Supplier company", value: t => t.supplier },
                    { label: "Account no.", value: t => t.supplier_account_number },
                    { label: "Payment method", value: t => t.default_payment_method },
                    { label: "Terms", value: t => t.payment_terms_notice },
                    { label: "Bank account", value: t => t.bank_account },
                  ],
                  null,
                  "id",
                  terms => {
                    openAdminEditor("Edit supplier payment settings", [
                      {
                        key: "company",
                        label: "Invoice company",
                        type: "select",
                        options: includeCurrentOption(
                          companyOptions(true),
                          terms.company,
                          terms.company === "*" ? "All invoice companies" : terms.company
                        ),
                        value: terms.company,
                        required: true,
                      },
                      {
                        key: "supplier",
                        label: "Supplier company",
                        type: "select",
                        options: includeCurrentOption(
                          supplierOptions(),
                          terms.supplier
                        ),
                        value: terms.supplier,
                        required: true,
                      },
                      {
                        key: "supplier_account_number",
                        label: "Supplier account number",
                        value: terms.supplier_account_number,
                        nullWhenBlank: true,
                      },
                      {
                        key: "default_payment_method",
                        label: "Default payment method",
                        value: terms.default_payment_method,
                        nullWhenBlank: true,
                      },
                      {
                        key: "payment_terms_notice",
                        label: "Payment terms",
                        value: terms.payment_terms_notice,
                        nullWhenBlank: true,
                      },
                      {
                        key: "bank_account",
                        label: "Bank account",
                        value: terms.bank_account,
                        nullWhenBlank: true,
                      },
                    ], async values => {
                      await putJson(`/api/admin/supplier-terms/${terms.id}`, values);
                      await loadAdminPanel();
                    });
                  }
                );
                renderCompanyGroupedTables(
                  document.getElementById("admin-matrix-table"),
                  rowsGroupedByCompany(visibleMatrix),
                  [
                    { label: "Supplier company", value: m => m.supplier },
                    {
                      label: "Approver 1",
                      value: m => m.approver1_email
                        ? `${m.approver1_name} <${m.approver1_email}>`
                        : `${m.approver1_name} — missing email`,
                    },
                    {
                      label: "Approver 2",
                      value: m => m.approver2_name
                        ? (m.approver2_email
                          ? `${m.approver2_name} <${m.approver2_email}>`
                          : `${m.approver2_name} — missing email`)
                        : "—",
                    },
                  ],
                  async id => {
                    await fetch(`/api/admin/approval-matrix/${id}`, { method: "DELETE" });
                    await loadAdminPanel();
                  },
                  "id",
                  entry => {
                    openAdminEditor("Edit approval route", [
                      {
                        key: "company",
                        label: "Invoice company",
                        type: "select",
                        options: includeCurrentOption(
                          companyOptions(true),
                          entry.company,
                          entry.company === "*" ? "All invoice companies" : entry.company
                        ),
                        value: entry.company,
                        required: true,
                      },
                      {
                        key: "supplier",
                        label: "Supplier company",
                        type: "select",
                        options: includeCurrentOption(
                          supplierOptions(),
                          entry.supplier
                        ),
                        value: entry.supplier,
                        required: true,
                      },
                      {
                        key: "approver1_name",
                        label: "Approver 1 name",
                        value: entry.approver1_name,
                        required: true,
                      },
                      {
                        key: "approver1_email",
                        label: "Approver 1 email",
                        type: "email",
                        value: entry.approver1_email,
                        required: true,
                      },
                      {
                        key: "approver2_name",
                        label: "Approver 2 name",
                        value: entry.approver2_name,
                        nullWhenBlank: true,
                      },
                      {
                        key: "approver2_email",
                        label: "Approver 2 email",
                        type: "email",
                        value: entry.approver2_email,
                        nullWhenBlank: true,
                      },
                    ], async values => {
                      await putJson(
                        `/api/admin/approval-matrix/${entry.id}`,
                        values
                      );
                      await loadAdminPanel();
                    });
                  }
                );
                document.getElementById("admin-threshold-value").value = threshold.threshold ?? 0.8;
                irjConfigurations = new Map(
                  irjSettings.map(setting => [setting.company, setting])
                );
                document.getElementById("admin-irj-settings").innerHTML =
                  '<table class="section-table"><thead><tr>' +
                  '<th>Company</th><th>Mode</th><th>Latest paper IRJ</th><th></th>' +
                  '</tr></thead><tbody>' +
                  irjSettings.map(setting => `
                    <tr>
                      <td>${escapeHtml(setting.company)}</td>
                      <td>
                        <select data-irj-mode="${escapeHtml(setting.company)}">
                          <option value="automatic" ${setting.mode === "automatic" ? "selected" : ""}>Automatic</option>
                          <option value="manual" ${setting.mode === "manual" ? "selected" : ""}>Manual at Sage</option>
                        </select>
                      </td>
                      <td>
                        <input
                          data-irj-current="${escapeHtml(setting.company)}"
                          inputmode="numeric"
                          pattern="[0-9]{6}"
                          maxlength="6"
                          value="${escapeHtml(setting.current_irj || "")}"
                          placeholder="e.g. 004321"
                          ${setting.mode === "manual" ? "disabled" : ""}
                        >
                      </td>
                      <td><button data-save-irj="${escapeHtml(setting.company)}">Save</button></td>
                    </tr>
                  `).join("") + '</tbody></table>';
                document.querySelectorAll("[data-irj-mode]").forEach(select => {
                  select.addEventListener("change", () => {
                    document.querySelector(
                      `[data-irj-current="${CSS.escape(select.dataset.irjMode)}"]`
                    ).disabled = select.value === "manual";
                  });
                });
                document.querySelectorAll("[data-save-irj]").forEach(button => {
                  button.addEventListener("click", async () => {
                    const company = button.dataset.saveIrj;
                    const mode = document.querySelector(
                      `[data-irj-mode="${CSS.escape(company)}"]`
                    ).value;
                    const currentInput = document.querySelector(
                      `[data-irj-current="${CSS.escape(company)}"]`
                    );
                    const currentIrj = currentInput.value.trim();
                    if (mode === "automatic" && currentIrj && !/^\\d{6}$/.test(currentIrj)) {
                      showToast("Latest paper IRJ must contain exactly six digits.", true);
                      return;
                    }
                    try {
                      await putJson(
                        `/api/admin/irj-configurations/${encodeURIComponent(company)}`,
                        { mode, current_irj: currentIrj || null }
                      );
                      showToast(`${company} IRJ settings saved.`);
                      await loadAdminPanel();
                    } catch (error) {
                      showToast(error.message, true);
                    }
                  });
                });
              } catch (error) {
                showToast(`Failed to load admin panel: ${error.message}`, true);
              }
            }

            document.getElementById("admin-import-form").addEventListener("submit", async event => {
              event.preventDefault();
              const fileInput = document.getElementById("admin-import-file");
              const companySelect = document.getElementById("admin-import-company");
              const resultBox = document.getElementById("admin-import-result");
              if (!companySelect.value || !fileInput.files.length) {
                return;
              }
              const formData = new FormData();
              formData.append("file", fileInput.files[0]);
              formData.append("company", companySelect.value);
              resultBox.innerHTML = "<p>Importing…</p>";
              try {
                let response = await fetch("/api/admin/import/supplier-master-data", {
                  method: "POST",
                  body: formData,
                });
                let body = await readResponseBody(response);
                if (response.status === 409 && body.detail?.duplicates) {
                  const duplicates = body.detail.duplicates
                    .map(item =>
                      `${item.existing_name} (spreadsheet row${item.row_numbers.length === 1 ? "" : "s"} ` +
                      `${item.row_numbers.join(", ")})`
                    )
                    .join("\\n");
                  const replace = window.confirm(
                    `The following supplier companies already exist:\n\n${duplicates}\n\n` +
                    `Replace their configured values with the spreadsheet rows?`
                  );
                  if (!replace) {
                    resultBox.innerHTML = "<p>Import cancelled; no records were changed.</p>";
                    return;
                  }
                  formData.set("replace_existing", "true");
                  response = await fetch("/api/admin/import/supplier-master-data", {
                    method: "POST",
                    body: formData,
                  });
                  body = await readResponseBody(response);
                }
                if (!response.ok) {
                  const detail = typeof body.detail === "string"
                    ? body.detail
                    : body.detail?.message;
                  throw new Error(detail || "Import failed.");
                }
                const issueRows = body.rows.filter(r => r.status !== "imported");
                const issueList = issueRows.length
                  ? "<ul>" +
                    issueRows
                      .map(r =>
                        `<li>Row ${escapeHtml(r.row_number)} (` +
                        `${escapeHtml(r.company === "*" ? "All invoice companies" : (r.company || "—"))} / ` +
                        `${escapeHtml(r.supplier || "—")}): ${escapeHtml(r.reason)}</li>`
                      )
                      .join("") +
                    "</ul>"
                  : "";
                resultBox.innerHTML =
                  `<p><strong>${escapeHtml(body.imported)}</strong> row(s) imported, ` +
                  `<strong>${escapeHtml(body.warnings)}</strong> warning(s), ` +
                  `<strong>${escapeHtml(body.skipped)}</strong> row(s) skipped.</p>${issueList}`;
                event.target.reset();
                await loadAdminPanel();
                await loadSuppliers();
              } catch (error) {
                resultBox.innerHTML = "";
                showToast(error.message, true);
              }
            });

            document.getElementById("admin-company-form").addEventListener("submit", async event => {
              event.preventDefault();
              try {
                await postJson("/api/admin/companies", {
                  name: document.getElementById("admin-company-name").value,
                  sharepoint_root_folder: document.getElementById("admin-company-root-folder").value,
                  aliases: splitCommaList(document.getElementById("admin-company-aliases").value),
                });
                event.target.reset();
                await loadAdminPanel();
              } catch (error) {
                showToast(error.message, true);
              }
            });

            document.getElementById("admin-company-root-folder").addEventListener("change", event => {
              const structure = sharePointCompanyFolders.get(event.target.value);
              const preview = document.getElementById("admin-company-folder-preview");
              if (!structure) {
                preview.textContent =
                  "Select a company folder to preview its workflow destinations.";
                return;
              }
              const rows = [
                ["Nominal invoices", structure.nominal_invoices],
                ["Approver 1", structure.nominal_approver_1],
                ["Approver 2", structure.nominal_approver_2],
                ["Nominal on hold", structure.nominal_on_hold],
                ["PO match", structure.po_match],
                ["PO on hold", structure.po_on_hold],
                ["Approved — BACS", structure.approved_bacs],
                ["Approved — BANKLINE", structure.approved_bankline],
                ["Approved — FOREIGN POA", structure.approved_foreign_poa],
                ["Paid", structure.paid],
                ["Reconciled", structure.reconciled],
              ];
              preview.innerHTML =
                "<table><tbody>" +
                rows.map(([label, path]) =>
                  `<tr><th>${escapeHtml(label)}</th><td>${escapeHtml(path)}</td></tr>`
                ).join("") +
                "</tbody></table>";
              const nameInput = document.getElementById("admin-company-name");
              if (!nameInput.value) {
                nameInput.value = structure.root.split("/").pop();
              }
            });

            document.getElementById("admin-supplier-form").addEventListener("submit", async event => {
              event.preventDefault();
              try {
                await postJson("/api/admin/suppliers", {
                  name: document.getElementById("admin-supplier-name").value,
                  aliases: splitCommaList(document.getElementById("admin-supplier-aliases").value),
                  default_company: document.getElementById("admin-supplier-default-company").value || null,
                  contact_email: document.getElementById("admin-supplier-contact").value || null,
                  invoice_number_pattern:
                    document.getElementById("admin-supplier-invoice-pattern").value || null,
                });
                event.target.reset();
                await loadAdminPanel();
                await loadSuppliers();
              } catch (error) {
                showToast(error.message, true);
              }
            });

            document.getElementById("admin-matrix-company").addEventListener(
              "change",
              () => refreshAdminMatrixSupplierOptions()
            );

            document.getElementById("admin-matrix-form").addEventListener("submit", async event => {
              event.preventDefault();
              try {
                await postJson("/api/admin/approval-matrix", {
                  company: document.getElementById("admin-matrix-company").value,
                  supplier: document.getElementById("admin-matrix-supplier").value,
                  approver1_name: document.getElementById("admin-matrix-approver1-name").value,
                  approver1_email: document.getElementById("admin-matrix-approver1-email").value,
                  approver2_name: document.getElementById("admin-matrix-approver2-name").value || null,
                  approver2_email: document.getElementById("admin-matrix-approver2-email").value || null,
                });
                event.target.reset();
                await loadAdminPanel();
              } catch (error) {
                showToast(error.message, true);
              }
            });

            document.getElementById("admin-threshold-form").addEventListener("submit", async event => {
              event.preventDefault();
              try {
                const threshold = Number(document.getElementById("admin-threshold-value").value);
                const response = await fetch("/api/admin/ai-threshold", {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ threshold }),
                });
                if (!response.ok) throw new Error("Failed to save threshold.");
                showToast("AI confidence threshold saved.");
              } catch (error) {
                showToast(error.message, true);
              }
            });

            document.getElementById("logout-button").addEventListener("click", async () => {
              if (authConfig.microsoft_enabled && !authConfig.local_enabled) {
                window.location.href = "/api/auth/microsoft/logout";
                return;
              }
              await fetch("/api/auth/logout", { method: "POST" });
              if (activityPollTimer !== null) window.clearInterval(activityPollTimer);
              if (invoicePollTimer !== null) window.clearInterval(invoicePollTimer);
              if (activityEventSource !== null) activityEventSource.close();
              activityPollTimer = null;
              invoicePollTimer = null;
              activityEventSource = null;
              currentUser = null;
              appRoot.classList.add("hidden");
              loginScreen.classList.remove("hidden");
            });

            document.getElementById("login-form").addEventListener("submit", async event => {
              event.preventDefault();
              if (!authConfig.local_enabled) return;
              const errorEl = document.getElementById("login-error");
              errorEl.textContent = "";
              const username = document.getElementById("login-username").value;
              const password = document.getElementById("login-password").value;
              try {
                const response = await fetch("/api/auth/login", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ username, password }),
                });
                if (!response.ok) {
                  const detail = await response.json().catch(() => ({}));
                  throw new Error(detail.detail || "Invalid username or password.");
                }
                await enterApp();
              } catch (error) {
                errorEl.textContent = error.message;
              }
            });

            async function loadAuthConfig() {
              const response = await fetch("/api/auth/config");
              if (!response.ok) return;
              authConfig = await response.json();
              const microsoftButton = document.getElementById("microsoft-login-button");
              const localFields = document.getElementById("local-login-fields");
              microsoftButton.classList.toggle(
                "hidden", !authConfig.microsoft_enabled
              );
              localFields.classList.toggle("hidden", !authConfig.local_enabled);
              document.getElementById("login-username").required =
                authConfig.local_enabled;
              document.getElementById("login-password").required =
                authConfig.local_enabled;
            }

            async function enterApp() {
              const response = await fetch("/api/auth/me");
              if (!response.ok) {
                loginScreen.classList.remove("hidden");
                appRoot.classList.add("hidden");
                return;
              }
              currentUser = await response.json();
              loginScreen.classList.add("hidden");
              appRoot.classList.remove("hidden");
              document.getElementById("user-chip-label").textContent =
                `${currentUser.display_name} (${currentUser.role})`;
              applyRoleVisibility(currentUser.role);
              await Promise.all([
                loadCompanies(),
                loadSearchSuppliers(),
                loadInvoices(),
              ]);
              await loadSuppliers();
              if (currentUser.role === "admin") {
                await loadAdminPanel();
              }
              await initActivityCursor();
              if (activityPollTimer !== null) window.clearInterval(activityPollTimer);
              if (invoicePollTimer !== null) window.clearInterval(invoicePollTimer);
              connectLiveUpdates();
              activityPollTimer = window.setInterval(pollActivity, 30000);
              invoicePollTimer = window.setInterval(loadInvoices, 120000);
            }

            document.addEventListener("visibilitychange", () => {
              if (document.visibilityState === "visible" && currentUser) {
                loadInvoices();
                pollActivity();
              }
            });

            document.getElementById("admin-bi-metrics").addEventListener(
              "toggle",
              event => {
                if (event.target.open) loadMetrics();
              }
            );

            document.getElementById("admin-operations").addEventListener(
              "toggle",
              event => {
                if (event.target.open) loadOperations();
              }
            );
            document.getElementById("refresh-operations").addEventListener(
              "click",
              loadOperations
            );
            document.getElementById("admin-matrix-filter").addEventListener(
              "input",
              () => {
                if (adminMatrixFilterTimer !== null) {
                  window.clearTimeout(adminMatrixFilterTimer);
                }
                adminMatrixFilterTimer = window.setTimeout(loadAdminPanel, 250);
              }
            );
            document.getElementById("admin-matrix-email-filter").addEventListener(
              "change",
              loadAdminPanel
            );

            document.getElementById("admin-companies-config").addEventListener(
              "toggle",
              event => {
                if (event.target.open && sharePointCompanyFolders.size === 0) {
                  loadSharePointFolderOptions();
                }
              }
            );
            document.getElementById("metrics-granularity").addEventListener(
              "change",
              loadMetrics
            );

            window.addEventListener("beforeunload", () => {
              if (activityEventSource !== null) activityEventSource.close();
            });

            loadAuthConfig().then(enterApp);
          </script>
        </body>
        </html>
        """

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _set_session_cookie(response: FastAPIResponse, token: str) -> None:
        secure_setting = os.environ.get("AUTH_COOKIE_SECURE", "").strip()
        secure = (
            secure_setting.casefold() in {"1", "true", "yes", "on"}
            if secure_setting
            else bool(app.state.entra_auth_client)
        )
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            httponly=True,
            secure=secure,
            samesite="lax",
            max_age=12 * 60 * 60,
        )

    @app.get("/api/auth/config")
    def auth_config() -> dict[str, bool]:
        return {
            "microsoft_enabled": app.state.entra_auth_client is not None,
            "local_enabled": app.state.local_login_enabled,
        }

    @app.post("/api/auth/login")
    def login(request: LoginRequest, response: FastAPIResponse) -> dict[str, object]:
        if not app.state.local_login_enabled:
            raise HTTPException(
                status_code=404,
                detail="Local password sign-in is disabled.",
            )
        try:
            user = app.state.auth_store.authenticate(request.username, request.password)
        except AuthError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        token = app.state.auth_store.create_session(user.username)
        _set_session_cookie(response, token)
        return {
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "role": user.role,
        }

    @app.get("/api/auth/microsoft/login")
    def microsoft_login() -> RedirectResponse:
        client = app.state.entra_auth_client
        if client is None:
            raise HTTPException(
                status_code=404,
                detail="Microsoft 365 sign-in is not configured.",
            )
        try:
            flow = client.initiate_flow()
            app.state.auth_store.save_oauth_flow(str(flow["state"]), flow)
        except AuthError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return RedirectResponse(str(flow["auth_uri"]), status_code=302)

    @app.get("/api/auth/microsoft/callback")
    def microsoft_callback(request: Request) -> RedirectResponse:
        client = app.state.entra_auth_client
        if client is None:
            raise HTTPException(
                status_code=404,
                detail="Microsoft 365 sign-in is not configured.",
            )
        state = request.query_params.get("state", "")
        flow = app.state.auth_store.pop_oauth_flow(state)
        if flow is None:
            raise HTTPException(
                status_code=400,
                detail="The Microsoft sign-in request expired or is invalid.",
            )
        try:
            user = client.complete_flow(flow, request.query_params)
        except AuthError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        token = app.state.auth_store.create_federated_session(user)
        response = RedirectResponse("/", status_code=302)
        _set_session_cookie(response, token)
        return response

    @app.get("/api/auth/microsoft/logout")
    def microsoft_logout(request: Request) -> RedirectResponse:
        client = app.state.entra_auth_client
        if client is None:
            return RedirectResponse("/", status_code=302)
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            app.state.auth_store.delete_session(token)
        response = RedirectResponse(client.logout_url(), status_code=302)
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request, response: FastAPIResponse) -> dict[str, str]:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            app.state.auth_store.delete_session(token)
        response.delete_cookie(SESSION_COOKIE_NAME)
        return {"status": "signed_out"}

    @app.get("/api/auth/me")
    def me(user: User = Depends(get_current_user)) -> dict[str, object]:
        return {
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "role": user.role,
        }

    @app.get("/api/health")
    def health() -> dict[str, object]:
        if app.state.worker_monitor is None:
            return {"status": "ok", "mode": "outlook-intake"}
        try:
            app.state.invoice_store.list(1)
            worker = app.state.worker_monitor.status()
        except Exception as error:
            logger.exception("Health check failed")
            return {
                "status": "unhealthy",
                "mode": "outlook-intake",
                "database": "unavailable",
                "detail": type(error).__name__,
            }
        stale_after = int(os.environ.get("WORKER_STALE_AFTER_SECONDS", "120"))
        worker_stale = worker is None or int(worker["age_seconds"]) > stale_after
        return {
            "status": "degraded" if worker_stale else "ok",
            "mode": "outlook-intake",
            "database": "ok",
            "worker": "stale" if worker_stale else str(worker["status"]),
            "worker_last_seen_at": worker["last_seen_at"] if worker else None,
        }

    def _irj_configuration(company: CompanyProfile) -> dict[str, object]:
        default_mode = (
            "manual"
            if company.name.strip().casefold() in {"swan", "cel"}
            else "automatic"
        )
        mode = app.state.process_configuration_store.get(
            f"irj_mode:{company.name.strip().casefold()}",
            default_mode,
        )
        return {
            "company": company.name,
            "mode": mode,
            "current_irj": app.state.irj_generator.current(company.name),
        }

    @staticmethod
    def _normalized_email(value: str | None) -> str:
        return (value or "").strip().casefold()

    def _invoice_is_visible_to_user(
        invoice: InvoiceRecord,
        user: User,
    ) -> bool:
        """Limit approvers to invoices assigned to their Entra identity."""
        if user.role not in {ROLE_APPROVER_1, ROLE_APPROVER_2}:
            return True
        user_email = _normalized_email(user.email)
        if not user_email:
            return False
        assigned_email = (
            invoice.approver1_email
            if user.role == ROLE_APPROVER_1
            else invoice.approver2_email
        )
        return _normalized_email(assigned_email) == user_email

    def _invoice_for_user(invoice_id: int, user: User) -> InvoiceRecord:
        invoice = app.state.invoice_store.get(invoice_id)
        if invoice is None or not _invoice_is_visible_to_user(invoice, user):
            # Use the same response for absent and inaccessible records so an
            # approver cannot discover another approver's invoice IDs.
            raise HTTPException(status_code=404, detail="Invoice was not found.")
        return invoice

    def _invoices_for_user(user: User, limit: int) -> list[InvoiceRecord]:
        if user.role == ROLE_APPROVER_1:
            return (
                app.state.invoice_store.list_for_approver(1, user.email, limit)
                if _normalized_email(user.email)
                else []
            )
        if user.role == ROLE_APPROVER_2:
            return (
                app.state.invoice_store.list_for_approver(2, user.email, limit)
                if _normalized_email(user.email)
                else []
            )
        return app.state.invoice_store.list(limit)

    def _require_assigned_approver(
        invoice_id: int,
        user: User,
        level: int,
    ) -> InvoiceRecord:
        invoice = app.state.invoice_store.get(invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice was not found.")
        if user.role == ROLE_ADMIN:
            return invoice
        expected_role = ROLE_APPROVER_1 if level == 1 else ROLE_APPROVER_2
        assigned_email = (
            invoice.approver1_email if level == 1 else invoice.approver2_email
        )
        if (
            user.role != expected_role
            or not _normalized_email(user.email)
            or _normalized_email(user.email) != _normalized_email(assigned_email)
        ):
            raise HTTPException(
                status_code=403,
                detail="This invoice is assigned to a different approver.",
            )
        return invoice

    @app.get("/api/irj-configurations")
    def irj_configurations(
        user: User = Depends(get_current_user),
    ) -> list[dict[str, object]]:
        return [
            _irj_configuration(company)
            for company in app.state.companies_store.list()
        ]

    @app.get("/api/companies")
    def companies(user: User = Depends(get_current_user)) -> list[dict[str, str]]:
        _sync_sharepoint_companies()
        return [
            {
                "name": profile.name,
                "company_folder": profile.company_folder,
                "po_matching_folder": profile.po_matching_folder,
            }
            for profile in app.state.companies_store.list()
        ]

    @app.get("/api/suppliers")
    def suppliers(
        company: str | None = Query(None),
        user: User = Depends(get_current_user),
    ) -> list[dict[str, object]]:
        profiles = app.state.suppliers_store.list()
        if company:
            company_key = company.strip().casefold()
            configured_suppliers = {
                entry.supplier.strip().casefold()
                for entry in app.state.approval_matrix_store.list()
                if entry.company == ALL_COMPANIES
                or entry.company.strip().casefold() == company_key
            }
            configured_suppliers.update(
                terms.supplier.strip().casefold()
                for terms in app.state.supplier_terms_store.list()
                if terms.company == ALL_COMPANIES
                or terms.company.strip().casefold() == company_key
            )
            profiles = [
                profile
                for profile in profiles
                if (
                    profile.default_company is not None
                    and profile.default_company.strip().casefold() == company_key
                )
                or profile.name.strip().casefold() in configured_suppliers
            ]
        return [asdict(profile) for profile in profiles]

    @app.get("/api/approval-matrix")
    def approval_matrix(user: User = Depends(get_current_user)) -> list[dict[str, object]]:
        return [
            {
                "company": entry.company,
                "supplier": entry.supplier,
                "approver1": {"name": entry.approver1.name, "email": entry.approver1.email},
                "approver2": (
                    {"name": entry.approver2.name, "email": entry.approver2.email}
                    if entry.approver2
                    else None
                ),
            }
            for entry in app.state.approval_matrix_store.list()
        ]

    @app.get("/api/supplier-terms")
    def supplier_terms(
        company: str,
        supplier: str,
        user: User = Depends(get_current_user),
    ) -> dict[str, object]:
        profiles = app.state.supplier_terms_store.list_for_supplier(company, supplier)
        return {"profiles": [asdict(terms) for terms in profiles]}

    @app.get("/api/activity")
    def activity(
        since_id: int = Query(0, ge=0), user: User = Depends(get_current_user)
    ) -> list[dict[str, object]]:
        events = app.state.activity_feed.list_since(since_id)
        if user.role in {ROLE_APPROVER_1, ROLE_APPROVER_2}:
            visible_events = []
            for event in events:
                if event.invoice_id is None:
                    continue
                invoice = app.state.invoice_store.get(event.invoice_id)
                if (
                    invoice is not None
                    and _invoice_is_visible_to_user(invoice, user)
                ):
                    visible_events.append(event)
            events = visible_events
        return [asdict(event) for event in events]

    @app.get("/api/activity/stream")
    async def activity_stream(
        request: Request,
        since_id: int = Query(0, ge=0),
        user: User = Depends(get_current_user),
    ) -> StreamingResponse:
        del user

        async def events():
            cursor = since_id
            heartbeat_ticks = 0
            while not await request.is_disconnected():
                latest = await asyncio.to_thread(
                    app.state.activity_feed.latest_id
                )
                if latest > cursor:
                    cursor = latest
                    yield f"event: invoice-update\ndata: {cursor}\n\n"
                    heartbeat_ticks = 0
                else:
                    heartbeat_ticks += 1
                    if heartbeat_ticks >= 30:
                        yield ": keep-alive\n\n"
                        heartbeat_ticks = 0
                await asyncio.sleep(2)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/invoices")
    def list_invoices(
        limit: int = Query(100, ge=1, le=500), user: User = Depends(get_current_user)
    ) -> list[dict[str, object]]:
        invoices = _invoices_for_user(user, limit)
        return [asdict(invoice) for invoice in invoices]

    @app.post("/api/invoices/manual-upload")
    async def manual_upload_invoice(
        file: UploadFile = File(...),
        sender_name: str | None = Form(None),
        sender_address: str | None = Form(None),
        subject: str | None = Form(None),
        received_at: str | None = Form(None),
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        """Manually add an invoice PDF into the same Incoming Invoices
        workflow used for Outlook-sourced invoices, per SOFTWARE_SPEC.md
        section 3: "There should also be a way of manually adding an
        invoice to the Incoming Invoices folder so that invoices received
        outside of the normal email process can still enter the same
        workflow."

        In the connected deployment, the PDF is uploaded to SharePoint
        Incoming first and registered from its DriveItem identity. A local
        cache is retained only for Document Intelligence and PDF preview.
        """
        if file.content_type not in {"application/pdf", "application/octet-stream"} and not (
            file.filename or ""
        ).lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDF files may be uploaded.")

        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="The uploaded file was empty.")
        try:
            validate_pdf(content)
        except InvalidPdfError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        unique_id = uuid.uuid4().hex
        original_filename = file.filename or f"manual-invoice-{unique_id}.pdf"

        message = {
            "internetMessageId": None,
            "subject": subject or "(Manually added invoice)",
            "receivedDateTime": received_at
            or datetime.now(timezone.utc).isoformat(),
            "from": {
                "emailAddress": {
                    "name": sender_name or "Manually added",
                    "address": sender_address or "",
                }
            },
        }

        lifecycle = _lifecycle()
        if lifecycle.sharepoint_client is not None:
            monitor = SharePointIncomingMonitor(
                lifecycle.sharepoint_client,
                app.state.invoice_store,
                cache_directory=Path(
                    os.environ.get(
                        "SHAREPOINT_INVOICE_CACHE_DIR",
                        "runtime_data/sharepoint_invoice_cache",
                    )
                ),
                extraction_runner=(
                    lifecycle.run_extraction
                    if ai_extraction_configured()
                    else lifecycle.run_document_classification
                ),
                activity_feed=app.state.activity_feed,
            )
            try:
                item = await asyncio.to_thread(
                    lifecycle.sharepoint_client.upload_to_incoming,
                    original_filename,
                    content,
                )
                record = await asyncio.to_thread(
                    monitor.ingest_item,
                    item,
                    source_message=message,
                    content=content,
                    event_type="manual_intake",
                )
            except InvoiceExtractionUnavailableError:
                item_id = str(item.get("id", ""))
                record = app.state.invoice_store.get_by_sharepoint_item_id(item_id)
            except SharePointError as error:
                raise HTTPException(
                    status_code=503,
                    detail=f"SharePoint Incoming upload failed: {error}",
                ) from error
            if record is None:
                raise HTTPException(
                    status_code=409,
                    detail="The SharePoint invoice is already registered.",
                )
        else:
            upload_dir = Path(os.environ.get("MANUAL_UPLOAD_DIR", "manual_uploads"))
            upload_dir.mkdir(parents=True, exist_ok=True)
            stored_path = upload_dir / f"{unique_id}-{original_filename}"
            stored_path.write_bytes(content)
            message["id"] = f"manual-{unique_id}"
            attachment = {
                "id": f"manual-attachment-{unique_id}",
                "name": original_filename,
                "size": len(content),
            }
            record = app.state.invoice_store.add_from_outlook(
                message=message, attachment=attachment, stored_path=stored_path
            )
            app.state.activity_feed.add_event(
                event_type="manual_intake",
                target_role=ROLE_PURCHASE_LEDGER,
                message=(
                    f"'{original_filename}' was manually added in offline mode "
                    "and is awaiting AI extraction."
                ),
                invoice_id=record.id,
            )
            if ai_extraction_configured():
                try:
                    record = lifecycle.run_extraction(record.id)
                except InvoiceExtractionUnavailableError:
                    record = app.state.invoice_store.get(record.id) or record
            else:
                record = lifecycle.run_document_classification(record.id)
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/register-sage")
    def register_invoice_in_sage(
        invoice_id: int,
        request: SageRegistrationRequest,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().register_in_sage(
                invoice_id,
                irj_number=request.irj_number,
                recorded_by=user.display_name,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/payment-route")
    def choose_payment_route(
        invoice_id: int,
        request: PaymentRouteRequest,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().route_for_payment(
                invoice_id,
                route=request.route,
                recorded_by=user.display_name,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/reject")
    def reject_invoice(
        invoice_id: int,
        request: RejectInvoiceRequest,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().reject_invoice(
                invoice_id, reason=request.reason, recorded_by=user.display_name
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/cancel-duplicate")
    def cancel_duplicate_invoice(
        invoice_id: int,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().cancel_confirmed_duplicate(
                invoice_id, recorded_by=user.display_name
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.get("/api/invoices/{invoice_id}")
    def get_invoice(invoice_id: int, user: User = Depends(get_current_user)) -> dict[str, object]:
        invoice = _invoice_for_user(invoice_id, user)
        return asdict(invoice)

    @app.delete("/api/invoices/{invoice_id}")
    def delete_invoice(
        invoice_id: int,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            _lifecycle().delete_invoice(
                invoice_id,
                recorded_by=user.display_name,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"id": invoice_id, "deleted": True}

    @app.get("/api/invoice-search")
    def search_invoice_by_irj(
        irj_number: str = Query(..., min_length=1, max_length=32),
        user: User = Depends(get_current_user),
    ) -> dict[str, object]:
        normalized = irj_number.strip()
        if len(normalized) != 6 or not normalized.isdigit():
            raise HTTPException(
                status_code=422,
                detail="An IRJ number must contain exactly six digits.",
            )
        invoice = app.state.invoice_store.get_by_irj_number(normalized)
        if invoice is None or not _invoice_is_visible_to_user(invoice, user):
            raise HTTPException(
                status_code=404,
                detail=f"No invoice was found for IRJ number '{normalized}'.",
            )
        result = asdict(invoice)
        result["audit_trail"] = [
            asdict(event)
            for event in app.state.activity_feed.list_for_invoice(invoice.id)
        ]
        return result

    @app.get("/api/invoice-search/filter")
    def filter_invoices(
        irj_number: str | None = Query(None, max_length=32),
        company: str | None = Query(None, max_length=255),
        supplier: str | None = Query(None, max_length=255),
        user: User = Depends(get_current_user),
    ) -> list[dict[str, object]]:
        normalized_irj = irj_number.strip() if irj_number else None
        normalized_company = company.strip().casefold() if company else None
        normalized_supplier = supplier.strip().casefold() if supplier else None
        if normalized_irj and (
            len(normalized_irj) != 6 or not normalized_irj.isdigit()
        ):
            raise HTTPException(
                status_code=422,
                detail="An IRJ number must contain exactly six digits.",
            )
        if not normalized_irj and not normalized_company and not normalized_supplier:
            raise HTTPException(
                status_code=422,
                detail="Provide an IRJ number, company, or supplier.",
            )
        matches = []
        candidates = (
            _invoices_for_user(user, 500)
            if user.role in {ROLE_APPROVER_1, ROLE_APPROVER_2}
            else app.state.invoice_store.list_all()
        )
        for invoice in candidates:
            if normalized_irj and invoice.irj_number != normalized_irj:
                continue
            if normalized_company and (
                not invoice.company
                or invoice.company.strip().casefold() != normalized_company
            ):
                continue
            if normalized_supplier and (
                not invoice.supplier
                or invoice.supplier.strip().casefold() != normalized_supplier
            ):
                continue
            result = asdict(invoice)
            if normalized_irj:
                result["audit_trail"] = [
                    asdict(event)
                    for event in app.state.activity_feed.list_for_invoice(invoice.id)
                ]
            matches.append(result)
        return matches

    @app.get("/api/statements")
    def list_statements(
        user: User = Depends(get_current_user),
    ) -> dict[str, list[dict[str, object]]]:
        client = _lifecycle().sharepoint_client
        if client is None:
            raise HTTPException(
                status_code=503,
                detail="SharePoint is not configured or accessible.",
            )
        try:
            return client.list_statement_library()
        except SharePointError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.get("/api/statements/{item_id}/pdf")
    def get_statement_pdf(
        item_id: str,
        user: User = Depends(get_current_user),
    ) -> Response:
        client = _lifecycle().sharepoint_client
        if client is None:
            raise HTTPException(
                status_code=503,
                detail="SharePoint is not configured or accessible.",
            )
        try:
            library = client.list_statement_library()
            known_statement_ids = {
                str(statement["id"])
                for statements in library.values()
                for statement in statements
            }
            if item_id not in known_statement_ids:
                raise HTTPException(
                    status_code=404,
                    detail="Statement PDF was not found.",
                )
            content = client.download_item(item_id)
            validate_pdf(content)
        except HTTPException:
            raise
        except (SharePointError, InvalidPdfError) as error:
            raise HTTPException(
                status_code=502,
                detail=f"Statement PDF could not be loaded: {error}",
            ) from error
        return Response(content=content, media_type="application/pdf")

    @app.get("/api/invoices/{invoice_id}/pdf")
    def get_invoice_pdf(
        invoice_id: int, user: User = Depends(get_current_user)
    ) -> FileResponse:
        invoice = _invoice_for_user(invoice_id, user)
        pdf_path = Path(invoice.stored_path)
        if not pdf_path.is_file():
            raise HTTPException(status_code=404, detail="Invoice PDF was not found.")
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=invoice.original_filename,
            content_disposition_type="inline",
        )

    @app.post("/api/invoices/{invoice_id}/extract")
    def extract_invoice(
        invoice_id: int, user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN))
    ) -> dict[str, object]:
        try:
            record = _lifecycle().run_extraction(invoice_id)
        except InvoiceExtractionUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/route-foreign-payment")
    def route_foreign_payment(
        invoice_id: int,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().route_as_foreign_payment(
                invoice_id, recorded_by=user.display_name
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/allocate-foreign-payment")
    def allocate_foreign_payment(
        invoice_id: int,
        request: ForeignAllocationRequest,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().mark_foreign_allocated(
                invoice_id,
                allocation_date=request.allocation_date,
                allocation_reference=request.allocation_reference,
                recorded_by=user.display_name,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/revert-foreign-payment")
    def revert_foreign_payment(
        invoice_id: int,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().revert_foreign_payment_route(
                invoice_id, recorded_by=user.display_name
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/confirm")
    def confirm_and_route_invoice(
        invoice_id: int,
        request: InvoiceConfirmRequest,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().confirm_and_route(
                invoice_id,
                company=request.company,
                supplier=request.supplier,
                supplier_invoice_number=request.supplier_invoice_number,
                purchase_order_number=request.purchase_order_number,
                invoice_date=request.invoice_date,
                invoice_value=request.invoice_value,
                currency=request.currency,
                override_duplicate=request.override_duplicate,
                recorded_by=user.display_name,
                correction_reason=request.correction_reason,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/po-match")
    def po_match_invoice(
        invoice_id: int,
        request: PoMatchRequest,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().record_po_match(
                invoice_id,
                matched=request.matched,
                notes=request.notes,
                query_category=request.query_category,
                purchasing_contact=request.purchasing_contact,
                recorded_by=user.display_name,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/approve")
    def approve_invoice(
        invoice_id: int,
        request: ApprovalDecisionRequest,
        user: User = Depends(require_role(*ROLES_APPROVERS_ADMIN)),
    ) -> dict[str, object]:
        # Approver 1 may only decide level-1 approvals, Approver 2 only
        # level-2 -- per MANUAL_VS_AUTOMATED.md each approver is a distinct
        # human decision step and must not be able to act on the other
        # level. Admin may act at either level (support/break-glass).
        if user.role == ROLE_APPROVER_1 and request.level != 1:
            raise HTTPException(
                status_code=403, detail="Approver 1 can only decide level-1 approvals."
            )
        if user.role == ROLE_APPROVER_2 and request.level != 2:
            raise HTTPException(
                status_code=403, detail="Approver 2 can only decide level-2 approvals."
            )
        _require_assigned_approver(invoice_id, user, request.level)
        try:
            record = _lifecycle().decide_approval(
                invoice_id,
                level=request.level,
                decision=request.decision,
                comments=request.comments,
                recorded_by=user.display_name,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/resume-approval")
    def resume_approval_invoice(
        invoice_id: int,
        request: ResumeApprovalRequest,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().resume_approval(
                invoice_id,
                resolution_notes=request.resolution_notes,
                recorded_by=user.display_name,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/flag-review")
    def flag_invoice_for_review(
        invoice_id: int,
        request: FlagReviewRequest,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().flag_for_review(invoice_id, reason=request.reason)
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/review-decision")
    def decide_flagged_invoice(
        invoice_id: int,
        request: ReviewDecisionRequest,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().resolve_flagged_review(
                invoice_id,
                accepted=request.accepted,
                reason=request.reason,
                recorded_by=user.display_name,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/reject-flagged")
    def reject_flagged_invoice(
        invoice_id: int,
        request: RejectInvoiceRequest,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().reject_flagged_invoice(
                invoice_id,
                reason=request.reason,
                recorded_by=user.display_name,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/file-statement")
    def file_supplier_statement(
        invoice_id: int,
        request: StatementFileRequest,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().file_statement(
                invoice_id,
                company=request.company,
                recorded_by=user.display_name,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/mark-as-invoice")
    def mark_document_as_invoice(
        invoice_id: int,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().mark_as_invoice(
                invoice_id,
                recorded_by=user.display_name,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/pay")
    def pay_invoice(
        invoice_id: int,
        request: PaymentRequest,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().mark_paid(
                invoice_id,
                payment_date=request.payment_date,
                supplier_account_number=request.supplier_account_number,
                payment_reference=request.payment_reference,
                payment_method=request.payment_method,
                recorded_by=user.display_name,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/reconcile")
    def reconcile_invoice(
        invoice_id: int,
        request: ReconciliationRequest,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().mark_reconciled(
                invoice_id,
                reconciliation_date=request.reconciliation_date,
                notes=request.notes,
                recorded_by=user.display_name,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    # ------------------------------------------------------------------
    # Admin configuration — Companies, Suppliers, Approval Matrix, AI
    # confidence threshold, and user management. All admin-only (see
    # SOFTWARE_SPEC.md: config must be maintainable by an
    # authorised staff member without touching code).
    # ------------------------------------------------------------------

    @app.get("/api/admin/companies")
    def admin_list_companies(
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> list[dict[str, object]]:
        _sync_sharepoint_companies()
        return [_company_response(profile) for profile in app.state.companies_store.list()]

    @app.get("/api/admin/sharepoint/folders")
    def admin_list_sharepoint_folders(
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, object]:
        client = _lifecycle().sharepoint_client
        if client is None:
            raise HTTPException(
                status_code=503,
                detail="SharePoint is not configured or accessible.",
            )
        try:
            folders = client.list_folder_paths()
        except SharePointError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        app.state.sharepoint_folder_paths = frozenset(folders)
        structures = discover_company_folder_structures(folders)
        app.state.sharepoint_company_structures = {
            structure.root: structure for structure in structures
        }
        return {
            "folders": folders,
            "company_roots": [structure.as_dict() for structure in structures],
            "shared_folders": {
                "incoming": INCOMING_INVOICES_FOLDER,
                "flagged": FLAGGED_INVOICES_FOLDER,
                "rejected": REJECTED_INVOICES_FOLDER,
            },
        }

    def _available_company_folder_structures() -> dict[str, CompanyFolderStructure]:
        client = _lifecycle().sharepoint_client
        if client is None:
            raise HTTPException(
                status_code=503,
                detail="SharePoint is not configured or accessible.",
            )
        try:
            folders = client.list_folder_paths()
        except SharePointError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return {
            structure.root: structure
            for structure in discover_company_folder_structures(folders)
        }

    @app.post("/api/admin/companies")
    def admin_create_company(
        request: CompanyRequest, user: User = Depends(require_role(ROLE_ADMIN))
    ) -> dict[str, object]:
        request_fields = request.model_dump()
        root = request_fields.pop("sharepoint_root_folder")
        structure: CompanyFolderStructure | None = None
        if root:
            structure = _available_company_folder_structures().get(root)
            if structure is None:
                raise HTTPException(
                    status_code=400,
                    detail="Select a complete company folder structure from SharePoint.",
                )
            request_fields["sharepoint_root_folder"] = structure.root
            request_fields["company_folder"] = structure.nominal_invoices
            request_fields["po_matching_folder"] = structure.po_match
        elif not request.company_folder or not request.po_matching_folder:
            raise HTTPException(
                status_code=400,
                detail="Select a SharePoint company folder.",
            )
        try:
            profile = app.state.companies_store.create(**request_fields)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return _company_response(profile)

    @app.put("/api/admin/companies/{name}")
    def admin_update_company(
        name: str,
        request: CompanyUpdateRequest,
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, object]:
        fields = request.model_dump(exclude_unset=True)
        root = fields.get("sharepoint_root_folder")
        if isinstance(root, str):
            structure = _available_company_folder_structures().get(root)
            if structure is None:
                raise HTTPException(
                    status_code=400,
                    detail="Select a complete company folder structure from SharePoint.",
                )
            fields.update(
                sharepoint_root_folder=structure.root,
                company_folder=structure.nominal_invoices,
                po_matching_folder=structure.po_match,
            )
        try:
            profile = app.state.companies_store.update(name, **fields)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return _company_response(profile)

    @app.delete("/api/admin/companies/{name}")
    def admin_delete_company(
        name: str, user: User = Depends(require_role(ROLE_ADMIN))
    ) -> dict[str, str]:
        try:
            app.state.companies_store.delete(name)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        app.state.approval_matrix_store.delete_by_company(name)
        app.state.supplier_terms_store.delete_by_company(name)
        return {"status": "deleted"}

    @app.get("/api/admin/suppliers")
    def admin_list_suppliers(
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> list[dict[str, object]]:
        return [asdict(profile) for profile in app.state.suppliers_store.list()]

    @app.post("/api/admin/suppliers")
    def admin_create_supplier(
        request: SupplierRequest, user: User = Depends(require_role(ROLE_ADMIN))
    ) -> dict[str, object]:
        try:
            profile = app.state.suppliers_store.create(**request.model_dump())
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(profile)

    @app.put("/api/admin/suppliers/{name}")
    def admin_update_supplier(
        name: str,
        request: SupplierUpdateRequest,
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, object]:
        fields = request.model_dump(exclude_unset=True)
        try:
            profile = app.state.suppliers_store.update(name, **fields)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(profile)

    @app.delete("/api/admin/suppliers/{name}")
    def admin_delete_supplier(
        name: str, user: User = Depends(require_role(ROLE_ADMIN))
    ) -> dict[str, str]:
        try:
            app.state.suppliers_store.delete(name)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        app.state.approval_matrix_store.delete_by_supplier(name)
        app.state.supplier_terms_store.delete_by_supplier(name)
        return {"status": "deleted"}

    @app.get("/api/admin/approval-matrix")
    def admin_list_approval_matrix(
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> list[dict[str, object]]:
        return [
            {
                "id": entry.id,
                "company": entry.company,
                "supplier": entry.supplier,
                "approver1_name": entry.approver1.name,
                "approver1_email": entry.approver1.email,
                "approver2_name": entry.approver2.name if entry.approver2 else None,
                "approver2_email": entry.approver2.email if entry.approver2 else None,
            }
            for entry in app.state.approval_matrix_store.list()
        ]

    @app.post("/api/admin/approval-matrix")
    def admin_create_approval_matrix_entry(
        request: ApprovalMatrixRequest, user: User = Depends(require_role(ROLE_ADMIN))
    ) -> dict[str, object]:
        try:
            fields = request.model_dump()
            existing = app.state.approval_matrix_store.find_exact(
                request.company, request.supplier
            )
            if existing is None:
                entry = app.state.approval_matrix_store.create(**fields)
            else:
                entry = app.state.approval_matrix_store.update(existing.id, **fields)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "id": entry.id,
            "company": entry.company,
            "supplier": entry.supplier,
            "approver1_name": entry.approver1.name,
            "approver1_email": entry.approver1.email,
            "approver2_name": entry.approver2.name if entry.approver2 else None,
            "approver2_email": entry.approver2.email if entry.approver2 else None,
        }

    @app.put("/api/admin/approval-matrix/{entry_id}")
    def admin_update_approval_matrix_entry(
        entry_id: int,
        request: ApprovalMatrixUpdateRequest,
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, object]:
        fields = request.model_dump(exclude_unset=True)
        try:
            entry = app.state.approval_matrix_store.update(entry_id, **fields)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "id": entry.id,
            "company": entry.company,
            "supplier": entry.supplier,
            "approver1_name": entry.approver1.name,
            "approver1_email": entry.approver1.email,
            "approver2_name": entry.approver2.name if entry.approver2 else None,
            "approver2_email": entry.approver2.email if entry.approver2 else None,
        }

    @app.delete("/api/admin/approval-matrix/{entry_id}")
    def admin_delete_approval_matrix_entry(
        entry_id: int, user: User = Depends(require_role(ROLE_ADMIN))
    ) -> dict[str, str]:
        try:
            app.state.approval_matrix_store.delete(entry_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"status": "deleted"}

    @app.get("/api/admin/supplier-terms")
    def admin_list_supplier_terms(
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> list[dict[str, object]]:
        return [asdict(terms) for terms in app.state.supplier_terms_store.list()]

    @app.get("/api/admin/metrics")
    def admin_metrics(
        granularity: str = Query("month", pattern="^(day|week|month)$"),
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, object]:
        del user
        return build_metrics(
            app.state.invoice_store.list_all(),
            app.state.supplier_terms_store.list(),
            granularity=granularity,
        )

    @app.get("/api/admin/operations")
    def admin_operations(
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, object]:
        del user
        worker = (
            app.state.worker_monitor.status()
            if app.state.worker_monitor is not None
            else None
        )
        queue = {
            status: [asdict(item) for item in app.state.notification_store.list(
                status=status, limit=50
            )]
            for status in ("pending", "processing", "failed")
        }
        stale_after = int(os.environ.get("WORKER_STALE_AFTER_SECONDS", "120"))
        alerts: list[str] = []
        if worker is None:
            alerts.append("The Outlook worker has not reported a heartbeat.")
        elif int(worker["age_seconds"]) > stale_after:
            alerts.append(
                f"The Outlook worker heartbeat is {worker['age_seconds']} seconds old."
            )
        if queue["failed"]:
            alerts.append(f"{len(queue['failed'])} Outlook queue item(s) have failed.")
        if queue["processing"]:
            alerts.append(
                f"{len(queue['processing'])} Outlook queue item(s) are processing."
            )
        return {"worker": worker, "queue": queue, "alerts": alerts}

    @app.post("/api/admin/outlook-queue/{notification_id}/retry")
    def admin_retry_outlook_notification(
        notification_id: int,
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, object]:
        del user
        try:
            app.state.notification_store.retry_failed(notification_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"id": notification_id, "status": "pending"}

    @app.put("/api/admin/supplier-terms/{terms_id}")
    def admin_update_supplier_terms(
        terms_id: int,
        request: SupplierTermsUpdateRequest,
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, object]:
        try:
            terms = app.state.supplier_terms_store.update(
                terms_id, **request.model_dump()
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(terms)

    @app.post("/api/admin/import/supplier-master-data")
    async def admin_import_supplier_master_data(
        file: UploadFile = File(...),
        company: str = Form(...),
        replace_existing: bool = Form(False),
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, object]:
        """Bulk-import supplier companies, approval matrix entries, and
        supplier payment terms from an uploaded .xlsx workbook, scoped to
        the company selected by the admin. A Company column in the workbook
        is ignored when this selection is provided. Expected columns (header
        names are flexible, see
        app/bulk_import.py): Trading Partner Name, Supplier Account Number,
        Default Payment Method, Payment Terms, Bank Account, Approver(s)."""
        if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
            raise HTTPException(
                status_code=422, detail="Please upload an Excel (.xlsx) file."
            )
        contents = await file.read()
        try:
            duplicates = find_existing_supplier_imports(
                io.BytesIO(contents),
                supplier_store=app.state.suppliers_store,
            )
            if duplicates and not replace_existing:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            "One or more supplier companies already exist. "
                            "Confirm before replacing their configured values."
                        ),
                        "duplicates": [asdict(duplicate) for duplicate in duplicates],
                    },
                )
            summary = import_supplier_workbook(
                io.BytesIO(contents),
                company_store=app.state.companies_store,
                supplier_store=app.state.suppliers_store,
                approval_matrix_store=app.state.approval_matrix_store,
                supplier_terms_store=app.state.supplier_terms_store,
                default_company=company,
            )
        except HTTPException:
            raise
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "imported": summary.imported_count,
            "warnings": summary.warning_count,
            "skipped": summary.skipped_count,
            "rows": [asdict(row) for row in summary.rows],
        }

    @app.get("/api/admin/ai-threshold")
    def admin_get_ai_threshold(user: User = Depends(require_role(ROLE_ADMIN))) -> dict[str, float]:
        from app.ai_extraction import confidence_threshold

        return {
            "threshold": confidence_threshold(
                app.state.process_configuration_store.get
            )
        }

    @app.put("/api/admin/ai-threshold")
    def admin_set_ai_threshold(
        request: ThresholdUpdateRequest, user: User = Depends(require_role(ROLE_ADMIN))
    ) -> dict[str, float]:
        if not 0.0 <= request.threshold <= 1.0:
            raise HTTPException(status_code=422, detail="Threshold must be between 0.0 and 1.0.")
        app.state.process_configuration_store.set(
            "ai_confidence_threshold", str(request.threshold)
        )
        return {"threshold": request.threshold}

    @app.put("/api/admin/irj-configurations/{company_name}")
    def admin_set_irj_configuration(
        company_name: str,
        request: IrjConfigurationUpdateRequest,
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, object]:
        company = app.state.companies_store.get(company_name)
        if company is None:
            raise HTTPException(
                status_code=404,
                detail=f"Company '{company_name}' was not found.",
            )
        mode = request.mode.strip().casefold()
        if mode not in {"automatic", "manual"}:
            raise HTTPException(
                status_code=422,
                detail="IRJ mode must be 'automatic' or 'manual'.",
            )
        if request.current_irj is not None:
            current_irj = request.current_irj.strip()
            if len(current_irj) != 6 or not current_irj.isdigit():
                raise HTTPException(
                    status_code=422,
                    detail="The latest paper IRJ must contain exactly six digits.",
                )
            assigned = [
                int(invoice.irj_number)
                for invoice in app.state.invoice_store.list_all()
                if invoice.company
                and invoice.company.casefold() == company.name.casefold()
                and invoice.irj_number
                and invoice.irj_number.isdigit()
            ]
            if assigned and int(current_irj) < max(assigned):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"The latest paper IRJ cannot be below the highest "
                        f"digitally assigned IRJ ({max(assigned):06d})."
                    ),
                )
            app.state.irj_generator.set_current(company.name, current_irj)
        app.state.process_configuration_store.set(
            f"irj_mode:{company.name.strip().casefold()}",
            mode,
        )
        return _irj_configuration(company)

    @app.post("/api/outlook/notifications")
    async def outlook_notifications(
        request: Request,
        validation_token: str | None = Query(None, alias="validationToken"),
    ) -> Response:
        if validation_token is not None:
            return PlainTextResponse(validation_token)

        expected_client_state = str(app.state.webhook_client_state)
        if not expected_client_state:
            raise HTTPException(
                status_code=503,
                detail="Outlook webhook client state is not configured.",
            )

        payload = await request.json()
        notifications = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(notifications, list):
            raise HTTPException(
                status_code=400, detail="Invalid Microsoft Graph notification payload."
            )

        for notification in notifications:
            if not isinstance(notification, dict):
                continue
            received_client_state = notification.get("clientState")
            if not isinstance(received_client_state, str) or not secrets.compare_digest(
                received_client_state, expected_client_state
            ):
                raise HTTPException(
                    status_code=401, detail="Invalid notification client state."
                )

            message_id = extract_message_id(notification)
            subscription_id = notification.get("subscriptionId")
            resource = notification.get("resource")
            change_type = notification.get("changeType")
            if not all(
                isinstance(value, str) and value
                for value in (
                    message_id,
                    subscription_id,
                    resource,
                    change_type,
                )
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Notification is missing required message data.",
                )

            app.state.notification_store.enqueue(
                subscription_id=subscription_id,
                message_id=message_id,
                resource=resource,
                change_type=change_type,
                payload=notification,
            )

        return Response(status_code=202)

    @app.post("/api/outlook/lifecycle")
    async def outlook_lifecycle(
        request: Request,
        validation_token: str | None = Query(None, alias="validationToken"),
    ) -> Response:
        if validation_token is not None:
            return PlainTextResponse(validation_token)

        expected_client_state = str(app.state.webhook_client_state)
        if not expected_client_state:
            raise HTTPException(
                status_code=503,
                detail="Outlook webhook client state is not configured.",
            )
        payload = await request.json()
        notifications = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(notifications, list):
            raise HTTPException(
                status_code=400, detail="Invalid lifecycle notification payload."
            )
        for notification in notifications:
            if not isinstance(notification, dict):
                continue
            received_client_state = notification.get("clientState")
            if not isinstance(received_client_state, str) or not secrets.compare_digest(
                received_client_state, expected_client_state
            ):
                raise HTTPException(
                    status_code=401,
                    detail="Invalid lifecycle notification client state.",
                )
        # Lifecycle persistence and alerting are added with production monitoring.
        return Response(status_code=202)

    @app.post("/api/workflow/confirm")
    def confirm_invoice(request: ConfirmationRequest) -> dict[str, str | None]:
        try:
            decision = route_confirmed_invoice(ConfirmedInvoice(**request.model_dump()))
        except RoutingValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "route": decision.route,
            "status": decision.status,
            "destination_folder": decision.destination_folder,
            "destination_filename": decision.destination_filename,
            "notification_recipient": decision.notification_recipient,
            "notification_reason": decision.notification_reason,
        }

    return app


app = create_app()
