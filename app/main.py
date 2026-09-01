from __future__ import annotations

import io
import logging
import os
import secrets
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response as FastAPIResponse, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel

from app.activity_feed import ActivityFeedStore, ROLE_PURCHASE_LEDGER
from app.approval_matrix import ApprovalMatrixStore
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
from app.bulk_import import import_supplier_workbook
from app.companies import CompanyStore
from app.config_db import config_database_path, set_setting
from app.environment import load_project_environment
from app.invoice_lifecycle import InvoiceLifecycle, InvoiceLifecycleError
from app.invoices import InvoiceStore
from app.irj import IrjNumberGenerator
from app.outlook_notifications import (
    OutlookNotificationStore,
    extract_message_id,
)
from app.sharepoint import SharePointClient
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


class PaymentRequest(BaseModel):
    payment_date: str
    payment_reference: str | None = None
    payment_method: str | None = None


class ReconciliationRequest(BaseModel):
    reconciliation_date: str
    notes: str | None = None


class ResumeApprovalRequest(BaseModel):
    resolution_notes: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class CompanyRequest(BaseModel):
    name: str
    company_folder: str
    po_matching_folder: str
    aliases: list[str] | None = None
    vat_number: str | None = None
    address: str | None = None


class CompanyUpdateRequest(BaseModel):
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


class SupplierUpdateRequest(BaseModel):
    aliases: list[str] | None = None
    default_company: str | None = None
    contact_email: str | None = None


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


class ThresholdUpdateRequest(BaseModel):
    threshold: float


class UserCreateRequest(BaseModel):
    username: str
    display_name: str
    email: str | None = None
    role: str
    password: str


def _build_invoice_store_from_environment(invoice_db_path: Path) -> InvoiceStore:
    """Choose the invoice metadata store backend.

    Defaults to the local SQLite store (INVOICE_STORE_BACKEND unset or
    "sqlite") so tests and prototype usage keep working with zero
    configuration. Set INVOICE_STORE_BACKEND=sharepoint_list to persist
    invoice metadata centrally in a SharePoint List instead -- see
    app/sharepoint_invoice_store.py for the required (currently
    placeholder) SHAREPOINT_INVOICES_SITE_ID / SHAREPOINT_INVOICES_LIST_ID
    environment variables.
    """
    backend = os.environ.get("INVOICE_STORE_BACKEND", "sqlite").strip().lower()
    if backend == "sqlite":
        return InvoiceStore(invoice_db_path)
    if backend == "sharepoint_list":
        from app.sharepoint_invoice_store import (
            sharepoint_invoice_store_from_environment,
        )

        return sharepoint_invoice_store_from_environment()  # type: ignore[return-value]
    raise ValueError(
        f"Unknown INVOICE_STORE_BACKEND '{backend}'. Expected 'sqlite' or "
        "'sharepoint_list'."
    )


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
) -> FastAPI:
    app = FastAPI(title="Invoice Intake Prototype", version="0.1.0")
    app.state.notification_store = notification_store or OutlookNotificationStore(
        Path(
            os.environ.get(
                "OUTLOOK_WEBHOOK_DB_PATH",
                "runtime_data/outlook_notifications.db",
            )
        )
    )
    invoice_db_path = Path(os.environ.get("INVOICE_DB_PATH", "runtime_data/invoices.db"))
    invoice_store_backend = os.environ.get("INVOICE_STORE_BACKEND", "sqlite").strip().lower()
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
    app.state.irj_generator = irj_generator or IrjNumberGenerator(irj_db_path)
    app.state.activity_feed = activity_feed or ActivityFeedStore(
        Path(os.environ.get("ACTIVITY_FEED_DB_PATH", "runtime_data/activity_feed.db"))
    )
    app.state.auth_store = auth_store or auth_store_from_environment()
    app.state.companies_store = companies_store or CompanyStore(
        Path(os.environ.get("CONFIG_DB_PATH", "runtime_data/config.db"))
    )
    app.state.suppliers_store = suppliers_store or SupplierStore(
        Path(os.environ.get("CONFIG_DB_PATH", "runtime_data/config.db"))
    )
    app.state.approval_matrix_store = approval_matrix_store or ApprovalMatrixStore(
        Path(os.environ.get("CONFIG_DB_PATH", "runtime_data/config.db"))
    )
    app.state.supplier_terms_store = supplier_terms_store or SupplierTermsStore(
        Path(os.environ.get("CONFIG_DB_PATH", "runtime_data/config.db"))
    )
    app.state.lifecycle = InvoiceLifecycle(
        app.state.invoice_store,
        app.state.irj_generator,
        app.state.activity_feed,
        app.state.sharepoint_client,
        companies_store=app.state.companies_store,
        approval_matrix_store=app.state.approval_matrix_store,
    )
    app.state.sharepoint_attach_attempted = app.state.sharepoint_client is not None

    logger.info(
        "Invoice Processor starting: invoice store backend=%s, SharePoint filing=%s",
        invoice_store_backend,
        "pre-configured" if app.state.sharepoint_client is not None else "not yet configured",
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

    @app.get("/", response_class=HTMLResponse)
    def prototype_home() -> str:
        return """
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>Invoice Review Workspace</title>
          <style>
            :root {
              color-scheme: light;
              font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
              color: #182230;
              background: #f4f7fb;
            }
            * { box-sizing: border-box; }
            body { margin: 0; min-width: 320px; }
            header {
              display: flex; align-items: center; justify-content: space-between;
              gap: 1rem; padding: 1rem 1.5rem; color: white;
              background: #102a43; border-bottom: 4px solid #2f80ed;
            }
            header h1 { font-size: 1.15rem; margin: 0; }
            .header-controls { display: flex; align-items: center; gap: .75rem; }
            .prototype {
              padding: .4rem .7rem; border: 1px solid #90cdf4; border-radius: 999px;
              color: #bee3f8; font-size: .78rem; font-weight: 700;
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
            nav.section-nav {
              display: flex; gap: .35rem; flex-wrap: wrap; padding: .6rem 1.5rem;
              background: white; border-bottom: 1px solid #d9e2ec;
            }
            nav.section-nav button {
              border: 1px solid #d9e2ec; background: #f8fafc; color: #486581;
              border-radius: 999px; padding: .45rem .85rem; font-size: .8rem;
              font-weight: 700; cursor: pointer;
            }
            nav.section-nav button.active { background: #2f80ed; color: white; border-color: #2f80ed; }
            nav.section-nav button .count {
              display: inline-block; margin-left: .35rem; padding: 0 .4rem;
              border-radius: 999px; background: rgba(0,0,0,.12); font-size: .72rem;
            }
            nav.section-nav button.active .count { background: rgba(255,255,255,.25); }
            main { max-width: 1500px; margin: 0 auto; padding: 1.25rem; }
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
            button {
              border: 0; border-radius: 7px; padding: .65rem .9rem;
              font: inherit; font-weight: 700; cursor: pointer;
            }
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
            table.section-table th { color: #52606d; font-size: .72rem; text-transform: uppercase; }
            table.section-table tr:last-child td { border-bottom: 0; }
            .row-actions { display: flex; gap: .4rem; flex-wrap: wrap; }
            .row-actions button { padding: .4rem .6rem; font-size: .78rem; }
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
            @keyframes toast-in { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: translateY(0); } }
            @media (max-width: 950px) {
              .layout { grid-template-columns: 1fr; }
              .pdf-empty { min-height: 360px; }
            }
            @media (max-width: 620px) {
              .grid, .grid.three { grid-template-columns: 1fr; }
              header { align-items: flex-start; flex-direction: column; }
            }
            #login-screen {
              position: fixed; inset: 0; z-index: 2000; display: grid; place-items: center;
              background: #0b1f33;
            }
            #login-screen.hidden { display: none; }
            .login-card {
              width: min(360px, 90vw); background: white; border-radius: 12px;
              padding: 1.6rem; box-shadow: 0 20px 60px rgba(0,0,0,.35);
            }
            .login-card h1 { font-size: 1.1rem; margin: 0 0 .3rem; }
            .login-card p { margin: 0 0 1rem; color: #52606d; font-size: .82rem; }
            .login-card label { margin-bottom: .7rem; }
            #login-error { color: #c53030; font-size: .8rem; min-height: 1.1em; margin-bottom: .5rem; }
            #app-root.hidden { display: none; }
            .user-chip {
              display: flex; align-items: center; gap: .5rem;
              padding: .3rem .7rem; border-radius: 999px; border: 1px solid #325377;
              background: #1c3a5e; color: #bee3f8; font-size: .78rem; font-weight: 600;
            }
            .user-chip button {
              background: transparent; border: 1px solid rgba(255,255,255,.4); color: white;
              padding: .25rem .55rem; font-size: .72rem; border-radius: 999px;
            }
            .admin-block { margin-bottom: 1.5rem; }
            .admin-block h3 { margin: 0 0 .6rem; font-size: .95rem; }
            .admin-form { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: .8rem; }
            .admin-form input, .admin-form select { width: auto; flex: 1 1 140px; min-height: 36px; }
            .admin-form button { white-space: nowrap; }
          </style>
        </head>
        <body>
          <div id="toast-container"></div>

          <div id="login-screen">
            <form class="login-card" id="login-form">
              <h1>Invoice Processing</h1>
              <p>Sign in with your staff account to continue.</p>
              <div id="login-error"></div>
              <label>Username<input id="login-username" autocomplete="username" required></label>
              <label>Password<input id="login-password" type="password" autocomplete="current-password" required></label>
              <button type="submit" class="primary" style="width: 100%">Sign in</button>
            </form>
          </div>

          <div id="app-root" class="hidden">
          <header>
            <h1>Invoice Processing</h1>
            <div class="header-controls">
              <span class="user-chip" id="user-chip">
                <span id="user-chip-label">Signed in</span>
                <button type="button" id="logout-button">Sign out</button>
              </span>
              <div class="prototype">OUTLOOK INTAKE CONNECTED - AI NOT CONNECTED</div>
            </div>
          </header>
          <nav class="section-nav" id="section-nav">
            <button data-tab="incoming" class="active">Incoming<span class="count" id="count-incoming">0</span></button>
            <button data-tab="po-matching">PO Matching<span class="count" id="count-po-matching">0</span></button>
            <button data-tab="approver1">Approver 1<span class="count" id="count-approver1">0</span></button>
            <button data-tab="approver2">Approver 2<span class="count" id="count-approver2">0</span></button>
            <button data-tab="on-hold">On Hold / Query<span class="count" id="count-on-hold">0</span></button>
            <button data-tab="approved">Approved<span class="count" id="count-approved">0</span></button>
            <button data-tab="reconciliation">Bank Reconciliation<span class="count" id="count-reconciliation">0</span></button>
            <button data-tab="complete">Complete / Filed<span class="count" id="count-complete">0</span></button>
            <button data-tab="rejected">Rejected<span class="count" id="count-rejected">0</span></button>
            <button data-tab="admin">Admin</button>
          </nav>
          <main>
            <div class="tab-panel active" data-tab-panel="incoming">
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
                    <div class="notice">
                      <strong>i</strong>
                      <div id="intake-notice">Outlook email and PDF data will be loaded here. Invoice extraction is not connected yet.</div>
                    </div>

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

                    <h3 class="section-title">Invoice identity (AI extraction preview — read-only)</h3>
                    <div class="grid">
                      <label>IRJ number
                        <input id="preview-irj" disabled placeholder="Assigned before final filing">
                      </label>
                      <label>Company being invoiced
                        <input id="preview-company" disabled placeholder="Extracted company">
                      </label>
                      <label>Supplier
                        <input id="preview-supplier" disabled placeholder="Extracted supplier">
                      </label>
                      <label>Supplier invoice number
                        <input id="preview-supplier-invoice" disabled placeholder="Extracted invoice number">
                      </label>
                      <label>Purchase Order number
                        <input id="preview-po" disabled placeholder="PO number or not detected">
                      </label>
                      <label>Invoice date
                        <input id="preview-invoice-date" disabled placeholder="Extracted invoice date">
                      </label>
                    </div>

                    <h3 class="section-title">Invoice value (AI extraction preview — read-only)</h3>
                    <div class="grid three">
                      <label>Invoice value<input id="preview-value" disabled placeholder="0.00"></label>
                      <label>Currency<input id="preview-currency" disabled placeholder="Currency"></label>
                    </div>

                    <h3 class="section-title">AI extraction status</h3>
                    <div class="grid">
                      <label>AI processing status
                        <input id="processing-status" disabled placeholder="Waiting for invoice">
                      </label>
                      <label>Overall confidence
                        <div class="confidence">
                          <input disabled placeholder="Not calculated">
                          <small>—</small>
                        </div>
                      </label>
                      <label style="grid-column: 1 / -1">Review warnings
                        <textarea id="review-warnings" disabled placeholder="Missing, uncertain, or conflicting fields will be shown here."></textarea>
                      </label>
                    </div>

                    <h3 class="section-title">Purchase Ledger confirmation (editable — enter or correct any field below, then confirm)</h3>
                    <div class="grid">
                      <label>Company
                        <select id="confirm-company"><option value="">Select company…</option></select>
                      </label>
                      <label>Supplier
                        <input id="confirm-supplier" placeholder="Supplier name">
                      </label>
                      <label>Supplier invoice number
                        <input id="confirm-supplier-invoice" placeholder="Supplier's invoice number">
                      </label>
                      <label>Purchase Order number
                        <input id="confirm-po" placeholder="Enter PO number if one applies, even if not shown above">
                      </label>
                      <label>Invoice date
                        <input id="confirm-invoice-date" type="date">
                      </label>
                      <label>Invoice value
                        <input id="confirm-invoice-value" type="number" step="0.01" placeholder="0.00">
                      </label>
                      <label>Currency
                        <input id="confirm-currency" value="GBP">
                      </label>
                    </div>
                    <div class="notice" id="duplicate-warning" style="display: none; background: #fef2f2; border-color: #f5b5b5; color: #9b1c1c;">
                      <strong>⚠</strong>
                      <div id="duplicate-warning-text"></div>
                    </div>
                  </div>
                  <div class="actions">
                    <button class="secondary" id="flag-review-button">Flag for review</button>
                    <button class="danger" id="override-duplicate-button" style="display: none">This is not a duplicate — route anyway</button>
                    <button class="primary" id="confirm-invoice-button">Purchase Ledger: confirm invoice</button>
                  </div>
                </section>
              </div>
            </div>

            <div class="tab-panel" data-tab-panel="po-matching">
              <section class="card">
                <div class="card-header"><h2>Purchase Order Invoice Matching</h2></div>
                <div class="content" id="po-matching-table"></div>
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
                  <div class="admin-block">
                    <h3>Bulk import supplier master data</h3>
                    <p style="margin: 0 0 8px; color: #555;">
                      Upload an Excel (.xlsx) sheet with columns for Company, Supplier,
                      Supplier Account Number, Default Payment Method, Payment Terms,
                      Bank Account, and Approver(s) to create/update these records in bulk
                      instead of entering every row by hand.
                    </p>
                    <form class="admin-form" id="admin-import-form">
                      <input id="admin-import-file" type="file" accept=".xlsx,.xlsm" required>
                      <button type="submit" class="primary">Import workbook</button>
                    </form>
                    <div id="admin-import-result"></div>
                  </div>

                  <div class="admin-block">
                    <h3>Companies</h3>
                    <form class="admin-form" id="admin-company-form">
                      <input id="admin-company-name" placeholder="Company name" required>
                      <input id="admin-company-folder" placeholder="Company folder path" required>
                      <input id="admin-company-po-folder" placeholder="PO matching folder path" required>
                      <input id="admin-company-aliases" placeholder="Aliases (comma separated)">
                      <button type="submit" class="primary">Add company</button>
                    </form>
                    <div id="admin-companies-table"></div>
                  </div>

                  <div class="admin-block">
                    <h3>Suppliers</h3>
                    <form class="admin-form" id="admin-supplier-form">
                      <input id="admin-supplier-name" placeholder="Supplier name" required>
                      <input id="admin-supplier-aliases" placeholder="Aliases (comma separated)">
                      <input id="admin-supplier-default-company" placeholder="Default company (optional)">
                      <input id="admin-supplier-contact" placeholder="Contact email (optional)">
                      <button type="submit" class="primary">Add supplier</button>
                    </form>
                    <div id="admin-suppliers-table"></div>
                  </div>

                  <div class="admin-block">
                    <h3>Approval matrix</h3>
                    <form class="admin-form" id="admin-matrix-form">
                      <input id="admin-matrix-company" placeholder="Company" required>
                      <input id="admin-matrix-supplier" placeholder="Supplier" required>
                      <input id="admin-matrix-approver1-name" placeholder="Approver 1 name" required>
                      <input id="admin-matrix-approver1-email" placeholder="Approver 1 email" required>
                      <input id="admin-matrix-approver2-name" placeholder="Approver 2 name (optional)">
                      <input id="admin-matrix-approver2-email" placeholder="Approver 2 email (optional)">
                      <button type="submit" class="primary">Add entry</button>
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

                  <div class="admin-block">
                    <h3>Users</h3>
                    <form class="admin-form" id="admin-user-form">
                      <input id="admin-user-username" placeholder="Username" required>
                      <input id="admin-user-display-name" placeholder="Display name" required>
                      <input id="admin-user-email" placeholder="Email (optional)">
                      <select id="admin-user-role">
                        <option value="admin">Admin</option>
                        <option value="purchase_ledger">Purchase Ledger</option>
                        <option value="approver1">Approver 1</option>
                        <option value="approver2">Approver 2</option>
                        <option value="purchasing">Purchasing</option>
                      </select>
                      <input id="admin-user-password" placeholder="Password" type="password" required>
                      <button type="submit" class="primary">Add user</button>
                    </form>
                    <div id="admin-users-table"></div>
                  </div>
                </div>
              </section>
            </div>
          </main>
          </div>
          <script>
            const SECTION_STATUSES = {
              "incoming": ["Awaiting AI Extraction", "Needs Review"],
              "po-matching": ["Awaiting PO Matching", "PO Query / Matching Issue"],
              "approver1": ["Awaiting Approval 1"],
              "approver2": ["Awaiting Approval 2"],
              "on-hold": ["Approval Query / On Hold"],
              "approved": ["Approved"],
              "reconciliation": ["Paid / Awaiting Bank Reconciliation"],
              "complete": ["Reconciled / Complete"],
              "rejected": ["Rejected"],
            };
            // Which nav tabs each signed-in role may view. "admin" is a
            // config panel, not an invoice-status tab, and is only ever
            // shown to the admin role. Every other tab maps 1:1 onto
            // MANUAL_VS_AUTOMATED.md's manual decision steps.
            const ROLE_TABS = {
              "admin": ["incoming", "po-matching", "approver1", "approver2", "on-hold", "approved", "reconciliation", "complete", "rejected", "admin"],
              "purchase_ledger": ["incoming", "po-matching", "on-hold", "approved", "reconciliation", "complete", "rejected"],
              "approver1": ["approver1"],
              "approver2": ["approver2"],
              "purchasing": ["po-matching"],
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
            let currentTab = "incoming";
            let lastActivityId = null;
            let currentUser = null;

            function setValue(id, value) {
              const el = document.getElementById(id);
              if (el) el.value = value || "";
            }

            function showToast(message, isError) {
              const toast = document.createElement("div");
              toast.className = "toast" + (isError ? " error" : "");
              toast.textContent = message;
              toastContainer.appendChild(toast);
              setTimeout(() => toast.remove(), 5000);
            }

            function showInvoice(invoice) {
              documentBadge.textContent = invoice.original_filename;
              processingBadge.textContent = invoice.status;
              setValue("source-sender", invoice.sender_address || invoice.sender_name);
              setValue("source-received", invoice.received_at);
              setValue("source-subject", invoice.subject);
              setValue("processing-status", invoice.status);
              setValue("preview-irj", invoice.irj_number);
              setValue("preview-company", invoice.company);
              setValue("preview-supplier", invoice.supplier);
              setValue("preview-supplier-invoice", invoice.supplier_invoice_number);
              setValue("preview-po", invoice.po_number);
              setValue("preview-invoice-date", invoice.invoice_date);
              setValue("preview-value", invoice.invoice_value);
              setValue("preview-currency", invoice.currency);
              setValue("review-warnings", invoice.review_reason);
              document.getElementById("intake-notice").textContent =
                invoice.review_reason ||
                "This PDF and its email metadata were retrieved from Outlook. AI extraction has not run yet.";
              const duplicateWarning = document.getElementById("duplicate-warning");
              const overrideButton = document.getElementById("override-duplicate-button");
              if (invoice.duplicate_of_invoice_id) {
                duplicateWarning.style.display = "grid";
                document.getElementById("duplicate-warning-text").textContent =
                  invoice.review_reason ||
                  `Possible duplicate of invoice #${invoice.duplicate_of_invoice_id}.`;
                overrideButton.style.display = "";
              } else {
                duplicateWarning.style.display = "none";
                overrideButton.style.display = "none";
              }
              if (displayedInvoiceId !== invoice.id) {
                // Only (re)populate the editable confirm-* fields when the
                // displayed invoice actually changes. showInvoice() is also
                // called on every periodic refresh (loadInvoices runs every
                // 5s); without this guard it would keep stomping on
                // whatever the user is actively typing into these fields
                // with the invoice's last-saved (often blank) values.
                setValue("confirm-company", invoice.company || "");
                setValue("confirm-supplier", invoice.supplier);
                setValue("confirm-supplier-invoice", invoice.supplier_invoice_number);
                setValue("confirm-po", invoice.po_number);
                setValue("confirm-invoice-date", invoice.invoice_date);
                setValue("confirm-invoice-value", invoice.invoice_value);
                setValue("confirm-currency", invoice.currency || "GBP");
                pdfFrame.src = `/api/invoices/${invoice.id}/pdf`;
                displayedInvoiceId = invoice.id;
              }
              pdfFrame.style.display = "block";
              pdfEmpty.style.display = "none";
            }

            function incomingInvoices() {
              return invoices.filter(i => SECTION_STATUSES["incoming"].includes(i.status));
            }

            function refreshPicker() {
              const candidates = incomingInvoices();
              if (!candidates.length) return;
              const selectedId = picker.value;
              picker.innerHTML = "";
              for (const invoice of candidates) {
                const option = document.createElement("option");
                option.value = invoice.id;
                option.textContent = `${invoice.original_filename} — ${invoice.subject || "No subject"}`;
                picker.appendChild(option);
              }
              const selected = candidates.find(
                invoice => String(invoice.id) === selectedId
              ) || candidates[0];
              picker.value = String(selected.id);
              showInvoice(selected);
            }

            picker.addEventListener("change", () => {
              const selected = invoices.find(
                invoice => String(invoice.id) === picker.value
              );
              if (selected) showInvoice(selected);
            });

            async function loadInvoices() {
              const response = await fetch("/api/invoices?limit=500");
              if (!response.ok) {
                document.getElementById("intake-notice").textContent =
                  "Received invoices could not be loaded.";
                return;
              }
              invoices = await response.json();
              refreshPicker();
              renderAllSections();
              updateCounts();
            }

            async function loadCompanies() {
              const response = await fetch("/api/companies");
              if (!response.ok) return;
              const companies = await response.json();
              const select = document.getElementById("confirm-company");
              select.innerHTML = '<option value="">Select company…</option>';
              for (const company of companies) {
                const option = document.createElement("option");
                option.value = company.name;
                option.textContent = company.name;
                select.appendChild(option);
              }
            }

            function updateCounts() {
              for (const tab of Object.keys(SECTION_STATUSES)) {
                const count = invoices.filter(i => SECTION_STATUSES[tab].includes(i.status)).length;
                const el = document.getElementById(`count-${tab}`);
                if (el) el.textContent = String(count);
              }
            }

            function escapeHtml(value) {
              const div = document.createElement("div");
              div.textContent = value == null ? "" : String(value);
              return div.innerHTML;
            }

            function renderSectionTable(containerId, statuses, columns, actionsFn) {
              const container = document.getElementById(containerId);
              if (!container) return;
              const rows = invoices.filter(i => statuses.includes(i.status));
              if (!rows.length) {
                container.innerHTML = '<div class="empty-state">No invoices in this section.</div>';
                return;
              }
              let html = '<table class="section-table"><thead><tr>';
              for (const column of columns) html += `<th>${column.label}</th>`;
              html += "<th>Actions</th></tr></thead><tbody>";
              for (const invoice of rows) {
                html += "<tr>";
                for (const column of columns) {
                  html += `<td>${escapeHtml(column.value(invoice))}</td>`;
                }
                html += `<td class="row-actions">${actionsFn(invoice)}</td>`;
                html += "</tr>";
              }
              html += "</tbody></table>";
              container.innerHTML = html;
              container.querySelectorAll("[data-action]").forEach(button => {
                button.addEventListener("click", () => handleRowAction(button));
              });
            }

            const BASE_COLUMNS = [
              { label: "IRJ", value: i => i.irj_number || "—" },
              { label: "Company", value: i => i.company || "—" },
              { label: "Supplier", value: i => i.supplier || "—" },
              { label: "File", value: i => i.original_filename },
              { label: "Status", value: i => i.status },
            ];

            function pdfLinkButton(invoice) {
              return `<a href="/api/invoices/${invoice.id}/pdf" target="_blank" rel="noopener">View PDF</a>`;
            }

            function renderAllSections() {
              renderSectionTable(
                "po-matching-table",
                SECTION_STATUSES["po-matching"],
                [...BASE_COLUMNS, { label: "PO number", value: i => i.po_number || "—" }],
                invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="po-match" data-id="${invoice.id}">Mark matched</button>
                  <button data-action="po-query" data-id="${invoice.id}">Record query</button>
                `
              );
              renderSectionTable(
                "approver1-table",
                SECTION_STATUSES["approver1"],
                BASE_COLUMNS,
                invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="approve" data-level="1" data-id="${invoice.id}">Approve</button>
                  <button data-action="hold" data-level="1" data-id="${invoice.id}" class="secondary">Hold / query</button>
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
                  <button data-action="hold" data-level="2" data-id="${invoice.id}" class="secondary">Hold / query</button>
                  <button data-action="reject" data-level="2" data-id="${invoice.id}" class="danger">Reject</button>
                `
              );
              renderSectionTable(
                "on-hold-table",
                SECTION_STATUSES["on-hold"],
                [...BASE_COLUMNS, { label: "Hold reason", value: i => i.hold_reason || "—" }],
                invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="resume" data-id="${invoice.id}">Resume approval</button>
                `
              );
              renderSectionTable(
                "approved-table",
                SECTION_STATUSES["approved"],
                BASE_COLUMNS,
                invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="pay" data-id="${invoice.id}">Mark paid</button>
                `
              );
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
                  { label: "Reconciled", value: i => i.reconciliation_date || "—" },
                  { label: "Reconciled by", value: i => i.reconciled_by || "—" },
                ],
                invoice => pdfLinkButton(invoice)
              );
              renderSectionTable(
                "rejected-table",
                SECTION_STATUSES["rejected"],
                [...BASE_COLUMNS, { label: "Reason", value: i => i.rejection_reason || "—" }],
                invoice => pdfLinkButton(invoice)
              );
            }

            async function postJson(url, body) {
              const response = await fetch(url, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body || {}),
              });
              if (!response.ok) {
                const detail = await response.json().catch(() => ({}));
                throw new Error(detail.detail || `Request failed (${response.status}).`);
              }
              return response.json();
            }

            async function handleRowAction(button) {
              const action = button.dataset.action;
              const id = button.dataset.id;
              try {
                if (action === "po-match") {
                  await postJson(`/api/invoices/${id}/po-match`, { matched: true, notes: null });
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
                } else if (action === "pay") {
                  const paymentDate = window.prompt("Payment date (YYYY-MM-DD):", new Date().toISOString().slice(0, 10));
                  if (!paymentDate) return;
                  const paymentReference = window.prompt("Payment reference (optional):");
                  const paymentMethod = window.prompt("Payment method (e.g. BACS, CHAPS, card):");
                  await postJson(`/api/invoices/${id}/pay`, {
                    payment_date: paymentDate,
                    payment_reference: paymentReference || null,
                    payment_method: paymentMethod || null,
                  });
                } else if (action === "reconcile") {
                  const reconciliationDate = window.prompt("Reconciliation date (YYYY-MM-DD):", new Date().toISOString().slice(0, 10));
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

            document.getElementById("confirm-invoice-button").addEventListener("click", async () => {
              await submitConfirm(false);
            });

            document.getElementById("override-duplicate-button").addEventListener("click", async () => {
              await submitConfirm(true);
            });

            async function submitConfirm(overrideDuplicate) {
              const invoiceId = picker.value;
              if (!invoiceId) return;
              const company = document.getElementById("confirm-company").value;
              const supplier = document.getElementById("confirm-supplier").value;
              if (!company || !supplier) {
                showToast("Company and supplier are required.", true);
                return;
              }
              const body = {
                company,
                supplier,
                supplier_invoice_number: document.getElementById("confirm-supplier-invoice").value || null,
                purchase_order_number: document.getElementById("confirm-po").value || null,
                invoice_date: document.getElementById("confirm-invoice-date").value || null,
                invoice_value: document.getElementById("confirm-invoice-value").value
                  ? Number(document.getElementById("confirm-invoice-value").value)
                  : null,
                currency: document.getElementById("confirm-currency").value || "GBP",
                override_duplicate: overrideDuplicate,
              };
              try {
                await postJson(`/api/invoices/${invoiceId}/confirm`, body);
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
                statusEl.className = "success";
                statusEl.textContent = `Added to Incoming Invoices as '${record.original_filename}'.`;
                event.target.reset();
                showToast("Invoice manually added to Incoming Invoices.");
                await loadInvoices();
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
              currentTab = button.dataset.tab;
              document.querySelectorAll("#section-nav button").forEach(b => b.classList.toggle("active", b === button));
              document.querySelectorAll(".tab-panel").forEach(panel => {
                panel.classList.toggle("active", panel.dataset.tabPanel === currentTab);
              });
            });

            function applyRoleVisibility(role) {
              const allowedTabs = ROLE_TABS[role] || Object.keys(SECTION_STATUSES);
              document.querySelectorAll("#section-nav button[data-tab]").forEach(button => {
                const isAllowed = allowedTabs.includes(button.dataset.tab);
                button.style.display = isAllowed ? "" : "none";
              });
              if (!allowedTabs.includes(currentTab)) {
                const fallback = document.querySelector(`#section-nav button[data-tab="${allowedTabs[0]}"]`);
                if (fallback) fallback.click();
              }
            }

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

            // ---------------------------------------------------------
            // Authentication + admin config panel
            // ---------------------------------------------------------

            function splitCommaList(value) {
              return (value || "")
                .split(",")
                .map(part => part.trim())
                .filter(Boolean);
            }

            function renderSimpleTable(container, columns, rows, onDelete, idKey = "id") {
              if (!rows.length) {
                container.innerHTML = '<p class="empty">Nothing here yet.</p>';
                return;
              }
              const head = columns.map(c => `<th>${c.label}</th>`).join("") + (onDelete ? "<th></th>" : "");
              const body = rows
                .map(row => {
                  const cells = columns.map(c => `<td>${c.value(row) ?? "—"}</td>`).join("");
                  const deleteCell = onDelete
                    ? `<td><button class="danger" data-delete-id="${row[idKey]}">Delete</button></td>`
                    : "";
                  return `<tr>${cells}${deleteCell}</tr>`;
                })
                .join("");
              container.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
              if (onDelete) {
                container.querySelectorAll("[data-delete-id]").forEach(button => {
                  button.addEventListener("click", () => onDelete(button.dataset.deleteId));
                });
              }
            }

            async function loadAdminPanel() {
              try {
                const [companies, suppliers, matrix, threshold, users] = await Promise.all([
                  fetch("/api/admin/companies").then(r => r.json()),
                  fetch("/api/admin/suppliers").then(r => r.json()),
                  fetch("/api/admin/approval-matrix").then(r => r.json()),
                  fetch("/api/admin/ai-threshold").then(r => r.json()),
                  fetch("/api/admin/users").then(r => r.json()),
                ]);
                renderSimpleTable(
                  document.getElementById("admin-companies-table"),
                  [
                    { label: "Name", value: c => c.name },
                    { label: "Invoice folder", value: c => c.company_folder },
                    { label: "PO folder", value: c => c.po_matching_folder },
                    { label: "Aliases", value: c => (c.aliases || []).join(", ") },
                  ],
                  companies,
                  async name => {
                    await fetch(`/api/admin/companies/${encodeURIComponent(name)}`, { method: "DELETE" });
                    await loadAdminPanel();
                  },
                  "name"
                );
                renderSimpleTable(
                  document.getElementById("admin-suppliers-table"),
                  [
                    { label: "Name", value: s => s.name },
                    { label: "Aliases", value: s => (s.aliases || []).join(", ") },
                    { label: "Default company", value: s => s.default_company },
                    { label: "Contact", value: s => s.contact_email },
                  ],
                  suppliers,
                  async name => {
                    await fetch(`/api/admin/suppliers/${encodeURIComponent(name)}`, { method: "DELETE" });
                    await loadAdminPanel();
                  },
                  "name"
                );
                renderSimpleTable(
                  document.getElementById("admin-matrix-table"),
                  [
                    { label: "Company", value: m => m.company },
                    { label: "Supplier", value: m => m.supplier },
                    { label: "Approver 1", value: m => `${m.approver1_name} <${m.approver1_email}>` },
                    { label: "Approver 2", value: m => (m.approver2_name ? `${m.approver2_name} <${m.approver2_email}>` : "—") },
                  ],
                  matrix,
                  async id => {
                    await fetch(`/api/admin/approval-matrix/${id}`, { method: "DELETE" });
                    await loadAdminPanel();
                  }
                );
                document.getElementById("admin-threshold-value").value = threshold.threshold ?? 0.8;
                renderSimpleTable(
                  document.getElementById("admin-users-table"),
                  [
                    { label: "Username", value: u => u.username },
                    { label: "Display name", value: u => u.display_name },
                    { label: "Role", value: u => u.role },
                  ],
                  users,
                  async username => {
                    await fetch(`/api/admin/users/${encodeURIComponent(username)}`, { method: "DELETE" });
                    await loadAdminPanel();
                  },
                  "username"
                );
              } catch (error) {
                showToast(`Failed to load admin panel: ${error.message}`, true);
              }
            }

            document.getElementById("admin-import-form").addEventListener("submit", async event => {
              event.preventDefault();
              const fileInput = document.getElementById("admin-import-file");
              const resultBox = document.getElementById("admin-import-result");
              if (!fileInput.files.length) {
                return;
              }
              const formData = new FormData();
              formData.append("file", fileInput.files[0]);
              resultBox.innerHTML = "<p>Importing…</p>";
              try {
                const response = await fetch("/api/admin/import/supplier-master-data", {
                  method: "POST",
                  body: formData,
                });
                const body = await response.json();
                if (!response.ok) {
                  throw new Error(body.detail || "Import failed.");
                }
                const skippedRows = body.rows.filter(r => r.status === "skipped");
                const skippedList = skippedRows.length
                  ? "<ul>" +
                    skippedRows
                      .map(r => `<li>Row ${r.row_number} (${r.company || "—"} / ${r.supplier || "—"}): ${r.reason}</li>`)
                      .join("") +
                    "</ul>"
                  : "";
                resultBox.innerHTML =
                  `<p><strong>${body.imported}</strong> row(s) imported, ` +
                  `<strong>${body.skipped}</strong> row(s) skipped.</p>${skippedList}`;
                event.target.reset();
                await loadAdminPanel();
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
                  company_folder: document.getElementById("admin-company-folder").value,
                  po_matching_folder: document.getElementById("admin-company-po-folder").value,
                  aliases: splitCommaList(document.getElementById("admin-company-aliases").value),
                });
                event.target.reset();
                await loadAdminPanel();
              } catch (error) {
                showToast(error.message, true);
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
                });
                event.target.reset();
                await loadAdminPanel();
              } catch (error) {
                showToast(error.message, true);
              }
            });

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

            document.getElementById("admin-user-form").addEventListener("submit", async event => {
              event.preventDefault();
              try {
                await postJson("/api/admin/users", {
                  username: document.getElementById("admin-user-username").value,
                  display_name: document.getElementById("admin-user-display-name").value,
                  email: document.getElementById("admin-user-email").value || null,
                  role: document.getElementById("admin-user-role").value,
                  password: document.getElementById("admin-user-password").value,
                });
                event.target.reset();
                await loadAdminPanel();
              } catch (error) {
                showToast(error.message, true);
              }
            });

            document.getElementById("logout-button").addEventListener("click", async () => {
              await fetch("/api/auth/logout", { method: "POST" });
              currentUser = null;
              appRoot.classList.add("hidden");
              loginScreen.classList.remove("hidden");
            });

            document.getElementById("login-form").addEventListener("submit", async event => {
              event.preventDefault();
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
              await loadInvoices();
              await loadCompanies();
              if (currentUser.role === "admin") await loadAdminPanel();
              await initActivityCursor();
              setInterval(pollActivity, 3000);
              setInterval(loadInvoices, 5000);
            }

            enterApp();
          </script>
        </body>
        </html>
        """

    # ------------------------------------------------------------------
    # Authentication (see app/auth.py — local placeholder for Entra ID SSO)
    # ------------------------------------------------------------------

    @app.post("/api/auth/login")
    def login(request: LoginRequest, response: FastAPIResponse) -> dict[str, object]:
        try:
            user = app.state.auth_store.authenticate(request.username, request.password)
        except AuthError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        token = app.state.auth_store.create_session(user.username)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            max_age=12 * 60 * 60,
        )
        return {
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "role": user.role,
        }

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
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "outlook-intake"}

    @app.get("/api/companies")
    def companies(user: User = Depends(get_current_user)) -> list[dict[str, str]]:
        return [
            {
                "name": profile.name,
                "company_folder": profile.company_folder,
                "po_matching_folder": profile.po_matching_folder,
            }
            for profile in app.state.companies_store.list()
        ]

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

    @app.get("/api/activity")
    def activity(
        since_id: int = Query(0, ge=0), user: User = Depends(get_current_user)
    ) -> list[dict[str, object]]:
        return [asdict(event) for event in app.state.activity_feed.list_since(since_id)]

    @app.get("/api/invoices")
    def list_invoices(
        limit: int = Query(100, ge=1, le=500), user: User = Depends(get_current_user)
    ) -> list[dict[str, object]]:
        return [asdict(invoice) for invoice in app.state.invoice_store.list(limit)]

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

        The uploaded PDF is stored locally (mirroring how the Outlook
        worker stores downloaded attachments) and a synthetic
        message/attachment pair is created so the same
        InvoiceStore.add_from_outlook() call used by the Outlook worker can
        be reused unchanged -- the invoice then proceeds through AI
        extraction, confirmation, routing, approval, payment, and
        reconciliation exactly like an email-sourced invoice.
        """
        if file.content_type not in {"application/pdf", "application/octet-stream"} and not (
            file.filename or ""
        ).lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDF files may be uploaded.")

        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="The uploaded file was empty.")

        upload_dir = Path(os.environ.get("MANUAL_UPLOAD_DIR", "manual_uploads"))
        upload_dir.mkdir(parents=True, exist_ok=True)
        unique_id = uuid.uuid4().hex
        original_filename = file.filename or f"manual-invoice-{unique_id}.pdf"
        stored_path = upload_dir / f"{unique_id}-{original_filename}"
        stored_path.write_bytes(content)

        message = {
            "id": f"manual-{unique_id}",
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
                f"'{original_filename}' was manually added to Incoming Invoices "
                "and is awaiting AI extraction."
            ),
            invoice_id=record.id,
        )
        return asdict(record)

    @app.get("/api/invoices/{invoice_id}")
    def get_invoice(invoice_id: int, user: User = Depends(get_current_user)) -> dict[str, object]:
        invoice = app.state.invoice_store.get(invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice was not found.")
        return asdict(invoice)

    @app.get("/api/invoices/{invoice_id}/pdf")
    def get_invoice_pdf(
        invoice_id: int, user: User = Depends(get_current_user)
    ) -> FileResponse:
        invoice = app.state.invoice_store.get(invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice was not found.")
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
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
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
        try:
            record = _lifecycle().decide_approval(
                invoice_id,
                level=request.level,
                decision=request.decision,
                comments=request.comments,
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
                invoice_id, resolution_notes=request.resolution_notes
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
            raise HTTPException(status_code=404, detail=str(error)) from error
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
    # SOFTWARE_SPEC.md section 7: config must be maintainable by an
    # authorised staff member without touching code).
    # ------------------------------------------------------------------

    @app.get("/api/admin/companies")
    def admin_list_companies(
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> list[dict[str, object]]:
        return [asdict(profile) for profile in app.state.companies_store.list()]

    @app.post("/api/admin/companies")
    def admin_create_company(
        request: CompanyRequest, user: User = Depends(require_role(ROLE_ADMIN))
    ) -> dict[str, object]:
        try:
            profile = app.state.companies_store.create(**request.model_dump())
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(profile)

    @app.put("/api/admin/companies/{name}")
    def admin_update_company(
        name: str,
        request: CompanyUpdateRequest,
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, object]:
        fields = {key: value for key, value in request.model_dump().items() if value is not None}
        try:
            profile = app.state.companies_store.update(name, **fields)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(profile)

    @app.delete("/api/admin/companies/{name}")
    def admin_delete_company(
        name: str, user: User = Depends(require_role(ROLE_ADMIN))
    ) -> dict[str, str]:
        try:
            app.state.companies_store.delete(name)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
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
        fields = {key: value for key, value in request.model_dump().items() if value is not None}
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
            entry = app.state.approval_matrix_store.create(**request.model_dump())
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
        fields = {key: value for key, value in request.model_dump().items() if value is not None}
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

    @app.post("/api/admin/import/supplier-master-data")
    async def admin_import_supplier_master_data(
        file: UploadFile = File(...),
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> dict[str, object]:
        """Bulk-import companies, suppliers, approval matrix entries, and
        supplier payment terms from an uploaded .xlsx workbook, so an admin
        doesn't have to manually re-key every row from an existing Excel
        sheet. Expected columns (header names are flexible, see
        app/bulk_import.py): Company, Supplier, Supplier Account Number,
        Default Payment Method, Payment Terms, Bank Account, Approver(s)."""
        if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
            raise HTTPException(
                status_code=422, detail="Please upload an Excel (.xlsx) file."
            )
        contents = await file.read()
        try:
            summary = import_supplier_workbook(
                io.BytesIO(contents),
                company_store=app.state.companies_store,
                supplier_store=app.state.suppliers_store,
                approval_matrix_store=app.state.approval_matrix_store,
                supplier_terms_store=app.state.supplier_terms_store,
                auth_store=app.state.auth_store,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "imported": summary.imported_count,
            "skipped": summary.skipped_count,
            "rows": [asdict(row) for row in summary.rows],
        }

    @app.get("/api/admin/ai-threshold")
    def admin_get_ai_threshold(user: User = Depends(require_role(ROLE_ADMIN))) -> dict[str, float]:
        from app.ai_extraction import confidence_threshold

        return {"threshold": confidence_threshold()}

    @app.put("/api/admin/ai-threshold")
    def admin_set_ai_threshold(
        request: ThresholdUpdateRequest, user: User = Depends(require_role(ROLE_ADMIN))
    ) -> dict[str, float]:
        if not 0.0 <= request.threshold <= 1.0:
            raise HTTPException(status_code=422, detail="Threshold must be between 0.0 and 1.0.")
        set_setting(
            "ai_confidence_threshold", str(request.threshold), database_path=config_database_path()
        )
        return {"threshold": request.threshold}

    @app.get("/api/admin/users")
    def admin_list_users(
        user: User = Depends(require_role(ROLE_ADMIN)),
    ) -> list[dict[str, object]]:
        return [asdict(u) for u in app.state.auth_store.list_users()]

    @app.post("/api/admin/users")
    def admin_create_user(
        request: UserCreateRequest, user: User = Depends(require_role(ROLE_ADMIN))
    ) -> dict[str, object]:
        try:
            created = app.state.auth_store.create_user(**request.model_dump())
        except AuthError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(created)

    @app.delete("/api/admin/users/{username}")
    def admin_delete_user(
        username: str, user: User = Depends(require_role(ROLE_ADMIN))
    ) -> dict[str, str]:
        try:
            app.state.auth_store.delete_user(username)
        except AuthError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"status": "deleted"}

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
