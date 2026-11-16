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
from fastapi.staticfiles import StaticFiles
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
    def prototype_home() -> HTMLResponse:
        return HTMLResponse(_index_html())

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

    @app.post("/api/invoices/{invoice_id}/retry-payment-route")
    def retry_invoice_payment_route(
        invoice_id: int,
        user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
    ) -> dict[str, object]:
        try:
            record = _lifecycle().auto_route_for_payment(invoice_id)
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
