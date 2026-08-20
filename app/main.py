from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel

from app.activity_feed import ActivityFeedStore, ROLE_PURCHASE_LEDGER
from app.approval_matrix import list_matrix
from app.companies import list_companies
from app.environment import load_project_environment
from app.invoice_lifecycle import InvoiceLifecycle, InvoiceLifecycleError
from app.invoices import InvoiceStore
from app.irj import IrjNumberGenerator
from app.outlook_notifications import (
    OutlookNotificationStore,
    extract_message_id,
)
from app.sharepoint import SharePointClient
from app.workflow import (
    ConfirmedInvoice,
    RoutingValidationError,
    route_confirmed_invoice,
)

load_project_environment()


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


class ApprovalDecisionRequest(BaseModel):
    level: int
    decision: str
    comments: str | None = None


class PoMatchRequest(BaseModel):
    matched: bool
    notes: str | None = None


class FlagReviewRequest(BaseModel):
    reason: str


class PaymentRequest(BaseModel):
    payment_date: str
    payment_reference: str | None = None


class ReconciliationRequest(BaseModel):
    reconciliation_date: str
    notes: str | None = None


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
    app.state.invoice_store = invoice_store or _build_invoice_store_from_environment(
        invoice_db_path
    )
    app.state.webhook_client_state = (
        webhook_client_state
        if webhook_client_state is not None
        else os.environ.get("OUTLOOK_WEBHOOK_CLIENT_STATE", "")
    )
    app.state.sharepoint_client = sharepoint_client
    app.state.irj_generator = irj_generator or IrjNumberGenerator(invoice_db_path)
    app.state.activity_feed = activity_feed or ActivityFeedStore(
        Path(os.environ.get("ACTIVITY_FEED_DB_PATH", "runtime_data/activity_feed.db"))
    )
    app.state.lifecycle = InvoiceLifecycle(
        app.state.invoice_store,
        app.state.irj_generator,
        app.state.activity_feed,
        app.state.sharepoint_client,
    )

    def _lifecycle() -> InvoiceLifecycle:
        # Lazily attach a SharePoint client from the environment the first
        # time it is needed, so tests that never touch SharePoint never pay
        # for constructing one, but real deployments pick up .env values
        # without an explicit override.
        current: InvoiceLifecycle = app.state.lifecycle
        if current.sharepoint_client is None and app.state.sharepoint_client is None:
            try:
                from app.sharepoint import sharepoint_client_from_environment

                client = sharepoint_client_from_environment()
                app.state.sharepoint_client = client
                current.sharepoint_client = client
            except Exception:
                pass
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
          </style>
        </head>
        <body>
          <div id="toast-container"></div>
          <header>
            <h1>Invoice Processing</h1>
            <div class="header-controls">
              <label class="role-switcher-label" for="role-switcher">
                🧪 Testing: view as role
                <select id="role-switcher" class="role-switcher">
                  <option value="purchase_ledger">Purchase Ledger</option>
                  <option value="approver1">Approver 1</option>
                  <option value="approver2">Approver 2</option>
                  <option value="purchasing">Purchasing</option>
                </select>
              </label>
              <div class="prototype">OUTLOOK INTAKE CONNECTED - AI NOT CONNECTED</div>
            </div>
          </header>
          <nav class="section-nav" id="section-nav">
            <button data-tab="incoming" class="active">Incoming<span class="count" id="count-incoming">0</span></button>
            <button data-tab="po-matching">PO Matching<span class="count" id="count-po-matching">0</span></button>
            <button data-tab="approver1">Approver 1<span class="count" id="count-approver1">0</span></button>
            <button data-tab="approver2">Approver 2<span class="count" id="count-approver2">0</span></button>
            <button data-tab="approved">Approved<span class="count" id="count-approved">0</span></button>
            <button data-tab="reconciliation">Bank Reconciliation<span class="count" id="count-reconciliation">0</span></button>
            <button data-tab="complete">Complete / Filed<span class="count" id="count-complete">0</span></button>
            <button data-tab="rejected">Rejected<span class="count" id="count-rejected">0</span></button>
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

                    <h3 class="section-title">Invoice identity</h3>
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

                    <h3 class="section-title">Invoice value</h3>
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

                    <h3 class="section-title">Purchase Ledger confirmation</h3>
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
                        <input id="confirm-po" placeholder="Leave blank if none">
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
                  </div>
                  <div class="actions">
                    <button class="secondary" id="flag-review-button">Flag for review</button>
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
          </main>
          <script>
            const SECTION_STATUSES = {
              "incoming": ["Awaiting AI Extraction", "Needs Review"],
              "po-matching": ["Awaiting PO Matching", "PO Query / Matching Issue"],
              "approver1": ["Awaiting Approval 1"],
              "approver2": ["Awaiting Approval 2"],
              "approved": ["Approved"],
              "reconciliation": ["Paid / Awaiting Bank Reconciliation"],
              "complete": ["Reconciled / Complete"],
              "rejected": ["Rejected"],
            };
            const ROLE_TABS = {
              "purchase_ledger": ["incoming", "po-matching", "approved", "reconciliation", "complete", "rejected"],
              "approver1": ["approver1"],
              "approver2": ["approver2"],
              "purchasing": ["po-matching"],
            };

            const picker = document.getElementById("invoice-picker");
            const pdfFrame = document.getElementById("pdf-frame");
            const pdfEmpty = document.getElementById("pdf-empty");
            const documentBadge = document.getElementById("document-badge");
            const processingBadge = document.getElementById("processing-badge");
            const roleSwitcher = document.getElementById("role-switcher");
            const toastContainer = document.getElementById("toast-container");
            let invoices = [];
            let displayedInvoiceId = null;
            let currentTab = "incoming";
            let lastActivityId = null;

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
              if (displayedInvoiceId !== invoice.id) {
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
                  <button data-action="reject" data-level="2" data-id="${invoice.id}" class="danger">Reject</button>
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
                [...BASE_COLUMNS, { label: "Payment date", value: i => i.payment_date || "—" }],
                invoice => `
                  ${pdfLinkButton(invoice)}
                  <button data-action="reconcile" data-id="${invoice.id}">Mark reconciled</button>
                `
              );
              renderSectionTable(
                "complete-table",
                SECTION_STATUSES["complete"],
                [...BASE_COLUMNS, { label: "Reconciled", value: i => i.reconciliation_date || "—" }],
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
                  await postJson(`/api/invoices/${id}/po-match`, { matched: false, notes });
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
                } else if (action === "pay") {
                  const paymentDate = window.prompt("Payment date (YYYY-MM-DD):", new Date().toISOString().slice(0, 10));
                  if (!paymentDate) return;
                  const paymentReference = window.prompt("Payment reference (optional):");
                  await postJson(`/api/invoices/${id}/pay`, {
                    payment_date: paymentDate,
                    payment_reference: paymentReference || null,
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
              };
              try {
                await postJson(`/api/invoices/${invoiceId}/confirm`, body);
                showToast("Invoice routed successfully.");
                await loadInvoices();
              } catch (error) {
                showToast(error.message, true);
              }
            });

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

            roleSwitcher.addEventListener("change", () => {
              const role = roleSwitcher.value;
              const allowedTabs = ROLE_TABS[role] || Object.keys(SECTION_STATUSES);
              document.querySelectorAll("#section-nav button[data-tab]").forEach(button => {
                const isAllowed = allowedTabs.includes(button.dataset.tab);
                button.style.display = isAllowed ? "" : "none";
              });
              if (!allowedTabs.includes(currentTab)) {
                const fallback = document.querySelector(`#section-nav button[data-tab="${allowedTabs[0]}"]`);
                if (fallback) fallback.click();
              }
            });
            roleSwitcher.dispatchEvent(new Event("change"));

            async function pollActivity() {
              try {
                const url = lastActivityId === null
                  ? "/api/activity"
                  : `/api/activity?since_id=${lastActivityId}`;
                const response = await fetch(url);
                if (!response.ok) return;
                const events = await response.json();
                if (!events.length) return;
                const currentRole = roleSwitcher.value;
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

            loadInvoices();
            loadCompanies();
            initActivityCursor().then(() => {
              setInterval(pollActivity, 3000);
            });
            setInterval(loadInvoices, 5000);
          </script>
        </body>
        </html>
        """

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "outlook-intake"}

    @app.get("/api/companies")
    def companies() -> list[dict[str, str]]:
        return [
            {
                "name": profile.name,
                "company_folder": profile.company_folder,
                "po_matching_folder": profile.po_matching_folder,
            }
            for profile in list_companies()
        ]

    @app.get("/api/approval-matrix")
    def approval_matrix() -> list[dict[str, object]]:
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
            for entry in list_matrix()
        ]

    @app.get("/api/activity")
    def activity(since_id: int = Query(0, ge=0)) -> list[dict[str, object]]:
        return [asdict(event) for event in app.state.activity_feed.list_since(since_id)]

    @app.get("/api/invoices")
    def list_invoices(limit: int = Query(100, ge=1, le=500)) -> list[dict[str, object]]:
        return [asdict(invoice) for invoice in app.state.invoice_store.list(limit)]

    @app.post("/api/invoices/manual-upload")
    async def manual_upload_invoice(
        file: UploadFile = File(...),
        sender_name: str | None = Form(None),
        sender_address: str | None = Form(None),
        subject: str | None = Form(None),
        received_at: str | None = Form(None),
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
    def get_invoice(invoice_id: int) -> dict[str, object]:
        invoice = app.state.invoice_store.get(invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice was not found.")
        return asdict(invoice)

    @app.get("/api/invoices/{invoice_id}/pdf")
    def get_invoice_pdf(invoice_id: int) -> FileResponse:
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
    def extract_invoice(invoice_id: int) -> dict[str, object]:
        try:
            record = _lifecycle().run_extraction(invoice_id)
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/confirm")
    def confirm_and_route_invoice(
        invoice_id: int, request: InvoiceConfirmRequest
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
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/po-match")
    def po_match_invoice(invoice_id: int, request: PoMatchRequest) -> dict[str, object]:
        try:
            record = _lifecycle().record_po_match(
                invoice_id, matched=request.matched, notes=request.notes
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/approve")
    def approve_invoice(invoice_id: int, request: ApprovalDecisionRequest) -> dict[str, object]:
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

    @app.post("/api/invoices/{invoice_id}/flag-review")
    def flag_invoice_for_review(
        invoice_id: int, request: FlagReviewRequest
    ) -> dict[str, object]:
        try:
            record = _lifecycle().flag_for_review(invoice_id, reason=request.reason)
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/pay")
    def pay_invoice(invoice_id: int, request: PaymentRequest) -> dict[str, object]:
        try:
            record = _lifecycle().mark_paid(
                invoice_id,
                payment_date=request.payment_date,
                payment_reference=request.payment_reference,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

    @app.post("/api/invoices/{invoice_id}/reconcile")
    def reconcile_invoice(invoice_id: int, request: ReconciliationRequest) -> dict[str, object]:
        try:
            record = _lifecycle().mark_reconciled(
                invoice_id,
                reconciliation_date=request.reconciliation_date,
                notes=request.notes,
            )
        except InvoiceLifecycleError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(record)

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
