from __future__ import annotations

import os
import secrets
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel

from app.environment import load_project_environment
from app.invoices import InvoiceStore
from app.outlook_notifications import (
    OutlookNotificationStore,
    extract_message_id,
)
from app.workflow import (
    ConfirmedInvoice,
    RoutingValidationError,
    route_confirmed_invoice,
)

load_project_environment()


class ConfirmationRequest(BaseModel):
    invoice_id: str
    company: str
    company_folder: str
    original_filename: str
    irj_number: str
    purchase_order_number: str | None = None
    po_matching_folder: str | None = None
    purchase_ledger_recipient: str | None = None


def create_app(
    *,
    notification_store: OutlookNotificationStore | None = None,
    invoice_store: InvoiceStore | None = None,
    webhook_client_state: str | None = None,
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
    app.state.invoice_store = invoice_store or InvoiceStore(
        Path(os.environ.get("INVOICE_DB_PATH", "runtime_data/invoices.db"))
    )
    app.state.webhook_client_state = (
        webhook_client_state
        if webhook_client_state is not None
        else os.environ.get("OUTLOOK_WEBHOOK_CLIENT_STATE", "")
    )

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
            .prototype {
              padding: .4rem .7rem; border: 1px solid #90cdf4; border-radius: 999px;
              color: #bee3f8; font-size: .78rem; font-weight: 700;
            }
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
              font: inherit; font-weight: 700;
            }
            button:disabled { background: #d9e2ec; color: #829ab1; cursor: not-allowed; }
            .secondary:disabled { border: 1px solid #bcccdc; background: white; }
            .invoice-picker { margin-bottom: 1rem; }
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
          <header>
            <h1>Invoice Processing</h1>
            <div class="prototype">OUTLOOK INTAKE CONNECTED - AI NOT CONNECTED</div>
          </header>
          <main>
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

                  <h3 class="section-title">Source email</h3>
                  <div class="grid">
                    <label>Sender<input id="source-sender" disabled placeholder="Email sender"></label>
                    <label>Date received<input id="source-received" disabled placeholder="Received date and time"></label>
                    <label style="grid-column: 1 / -1">Email subject<input id="source-subject" disabled placeholder="Email subject"></label>
                  </div>

                  <h3 class="section-title">Invoice identity</h3>
                  <div class="grid">
                    <label>IRJ number
                      <input disabled placeholder="Assigned before final filing">
                    </label>
                    <label>Company being invoiced
                      <input disabled placeholder="Extracted company">
                    </label>
                    <label>Supplier
                      <input disabled placeholder="Extracted supplier">
                    </label>
                    <label>Supplier invoice number
                      <input disabled placeholder="Extracted invoice number">
                    </label>
                    <label>Purchase Order number
                      <input disabled placeholder="PO number or not detected">
                    </label>
                    <label>Invoice date
                      <input disabled placeholder="Extracted invoice date">
                    </label>
                    <label>Invoice route
                      <select disabled><option>Awaiting extraction</option></select>
                    </label>
                  </div>

                  <h3 class="section-title">Invoice values</h3>
                  <div class="grid three">
                    <label>Net amount<input disabled placeholder="0.00"></label>
                    <label>VAT amount<input disabled placeholder="0.00"></label>
                    <label>Total amount<input disabled placeholder="0.00"></label>
                    <label>Currency<input disabled placeholder="Currency"></label>
                    <label>Payment terms<input disabled placeholder="If available"></label>
                    <label>Due date<input disabled placeholder="If available"></label>
                  </div>

                  <h3 class="section-title">Extraction quality</h3>
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
                      <textarea disabled placeholder="Missing, uncertain, or conflicting fields will be shown here."></textarea>
                    </label>
                  </div>
                </div>
                <div class="actions">
                  <button class="secondary" disabled>Flag for review</button>
                  <button disabled>Purchase Ledger: confirm invoice</button>
                </div>
              </section>
            </div>
          </main>
          <script>
            const picker = document.getElementById("invoice-picker");
            const pdfFrame = document.getElementById("pdf-frame");
            const pdfEmpty = document.getElementById("pdf-empty");
            const documentBadge = document.getElementById("document-badge");
            const processingBadge = document.getElementById("processing-badge");
            let invoices = [];
            let displayedInvoiceId = null;

            function setValue(id, value) {
              document.getElementById(id).value = value || "";
            }

            function showInvoice(invoice) {
              documentBadge.textContent = invoice.original_filename;
              processingBadge.textContent = invoice.status;
              setValue("source-sender", invoice.sender_address || invoice.sender_name);
              setValue("source-received", invoice.received_at);
              setValue("source-subject", invoice.subject);
              setValue("processing-status", invoice.status);
              document.getElementById("intake-notice").textContent =
                "This PDF and its email metadata were retrieved from Outlook. AI extraction has not run yet.";
              if (displayedInvoiceId !== invoice.id) {
                pdfFrame.src = `/api/invoices/${invoice.id}/pdf`;
                displayedInvoiceId = invoice.id;
              }
              pdfFrame.style.display = "block";
              pdfEmpty.style.display = "none";
            }

            async function loadInvoices() {
              const response = await fetch("/api/invoices");
              if (!response.ok) {
                document.getElementById("intake-notice").textContent =
                  "Received invoices could not be loaded.";
                return;
              }
              invoices = await response.json();
              if (!invoices.length) return;
              const selectedId = picker.value;
              picker.innerHTML = "";
              for (const invoice of invoices) {
                const option = document.createElement("option");
                option.value = invoice.id;
                option.textContent = `${invoice.original_filename} — ${invoice.subject || "No subject"}`;
                picker.appendChild(option);
              }
              const selected = invoices.find(
                invoice => String(invoice.id) === selectedId
              ) || invoices[0];
              picker.value = String(selected.id);
              showInvoice(selected);
            }

            picker.addEventListener("change", () => {
              const selected = invoices.find(
                invoice => String(invoice.id) === picker.value
              );
              if (selected) showInvoice(selected);
            });
            loadInvoices();
            setInterval(loadInvoices, 5000);
          </script>
        </body>
        </html>
        """

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "outlook-intake"}

    @app.get("/api/invoices")
    def list_invoices(limit: int = Query(100, ge=1, le=500)) -> list[dict[str, object]]:
        return [asdict(invoice) for invoice in app.state.invoice_store.list(limit)]

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
