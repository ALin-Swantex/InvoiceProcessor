from typing import cast

from fastapi.testclient import TestClient

from app.activity_feed import ActivityFeedStore
from app.approval_matrix import ApprovalMatrixStore
from app.auth import AuthStore
from app.companies import CompanyStore
from app.invoices import InvoiceStore
from app.main import create_app
from app.outlook_notifications import OutlookNotificationStore
from app.sharepoint import SharePointClient
from app.suppliers import SupplierStore
from tests.pdf_helpers import VALID_PDF_BYTES


def client_for(tmp_path) -> TestClient:
    return TestClient(
        create_app(
            invoice_store=InvoiceStore(tmp_path / "invoices.db"),
            auth_store=AuthStore(tmp_path / "auth.db"),
            companies_store=CompanyStore(tmp_path / "config.db"),
            suppliers_store=SupplierStore(tmp_path / "config.db"),
            approval_matrix_store=ApprovalMatrixStore(tmp_path / "config.db"),
            activity_feed=ActivityFeedStore(tmp_path / "activity_feed.db"),
            notification_store=OutlookNotificationStore(
                tmp_path / "outlook_notifications.db"
            ),
        )
    )


def test_invoice_review_interface_contains_required_sections(tmp_path) -> None:
    client = client_for(tmp_path)

    home = client.get("/")
    assert home.status_code == 200
    assert "<h1>Swantex</h1>" in home.text
    assert "Continue with Microsoft 365" in home.text
    assert "Protected by Microsoft Entra ID" in home.text
    assert "OUTLOOK INTAKE CONNECTED - AI NOT CONNECTED" not in home.text
    assert '<aside class="sidebar">' in home.text
    assert 'data-tab="needs-review"' in home.text
    assert home.text.index('data-tab="search"') < home.text.index('data-tab="incoming"')
    assert home.text.index('data-tab="statements"') < home.text.index('data-tab="incoming"')
    assert 'data-tab="statements"' in home.text
    assert "Flagged Documents — Purchase Ledger Review" in home.text
    assert 'id="payment-method"' in home.text
    assert '<select id="admin-import-company" required>' in home.text
    assert "Supplier payment settings" in home.text
    assert "<h3>Users</h3>" not in home.text
    assert "<h3>Microsoft 365 roles</h3>" not in home.text
    assert 'id="admin-user-form"' not in home.text
    assert 'id="admin-bi-metrics"' in home.text
    assert 'id="metrics-granularity"' in home.text
    assert "Invoice volume interval" in home.text
    assert "Total pending invoice value by company" in home.text
    assert "Spend by supplier and company" in home.text
    assert "function loadMetrics()" in home.text
    assert "async function readResponseBody(response)" in home.text
    assert '<details class="admin-block admin-collapsible">' in home.text
    assert "<summary>Companies</summary>" in home.text
    assert 'id="admin-company-root-folder" required disabled' in home.text
    assert '<select id="admin-supplier-default-company">' in home.text
    assert 'id="admin-supplier-invoice-pattern"' in home.text
    assert "# = digit, @ = letter, * = letter or digit" in home.text
    assert '<select id="admin-matrix-company" required>' in home.text
    assert '<select id="admin-matrix-supplier" required>' in home.text
    assert "All invoice companies" in home.text
    assert 'id="admin-edit-dialog"' in home.text
    assert "function openAdminEditor" in home.text
    assert 'data-edit-index="${index}"' in home.text
    assert '${escapeHtml(c.value(row) ?? "—")}' in home.text
    assert 'data-delete-index="${index}"' in home.text
    assert "${escapeHtml(r.reason)}</li>" in home.text
    assert "supplier companies already exist" in home.text
    assert 'formData.set("replace_existing", "true")' in home.text
    assert '.join("\\n")' in home.text
    assert 'id="admin-company-folder-preview"' in home.text
    assert "Statement classification" in home.text
    assert "IRJ number" in home.text
    assert "Company being invoiced" in home.text
    assert '<select id="preview-company">' in home.text
    assert '<select id="preview-supplier" disabled>' in home.text
    assert "Purchase Ledger confirmation (editable" not in home.text
    assert "Invoice details (AI-extracted — review and correct before confirming)" in home.text
    assert "Outlook email and PDF data will be loaded here." not in home.text
    assert 'id="intake-notice"' not in home.text
    assert "function renderCompanyGroupedTables" in home.text
    assert "function suppliersGroupedByCompany" in home.text
    assert "function refreshAdminMatrixSupplierOptions" in home.text
    assert "/api/suppliers?company=${encodeURIComponent(company)}" in home.text
    assert "Supplier invoice number" in home.text
    assert "Purchase Order number" in home.text
    assert "Invoice value" in home.text
    assert "Overall confidence" in home.text
    assert "PDF and extracted fields" in home.text
    assert "function invoiceAnalysisHtml" in home.text
    assert "function invoiceHistoryHtml(invoice)" in home.text
    assert "function invoiceAuditHtml(invoice)" in home.text
    assert "function formatUkTimestamp(value)" in home.text
    assert 'timeZone: "Europe/London"' in home.text
    assert 'notes.join("\\n\\n")' in home.text
    assert '${invoice.approver1_name || "Approval"} note' in home.text
    assert '${invoice.approver2_name || "Approval"} note' in home.text
    assert "Description and history" in home.text
    assert "<h4>Audit trail</h4>" in home.text
    assert "Approval query / on hold" in home.text
    assert "Payment and reconciliation" in home.text
    assert 'id="supplier-match-prompt"' in home.text
    assert "function suggestedSupplier(extractedSupplier, suppliers)" in home.text
    assert "Is this" in home.text
    assert "not registered for" in home.text
    assert 'data-expand-invoice="${invoice.id}"' in home.text
    assert 'class="row-action-buttons"' in home.text
    assert 'class="action-link"' in home.text
    assert 'button.setAttribute("aria-busy", "true")' in home.text
    assert 'button.textContent = "Working…"' in home.text
    assert 'class="invoice-analysis-pdf"' in home.text
    assert "Extracted invoice analysis" in home.text
    assert "File as statement" in home.text
    assert 'data-action="reject-flagged"' in home.text
    assert "/api/invoices/${id}/reject-flagged" in home.text
    assert "/api/invoices/${id}/file-statement" in home.text
    assert "/api/invoices/${id}/mark-as-invoice" in home.text
    assert "Search invoices" in home.text
    assert 'data-tab="payment-bacs"' in home.text
    assert 'data-tab="payment-bankline"' in home.text
    assert 'data-tab="payment-foreign-poa"' in home.text
    assert "Appeared on bank statement" in home.text
    assert "Marked reconciled" in home.text
    assert "Uploaded '${record.original_filename}' to ${destination}." in home.text
    assert "Statements/{Company}" in home.text
    assert 'id="statement-company"' in home.text
    assert 'fetch("/api/statements")' in home.text
    assert 'const ACTIVE_TAB_STORAGE_KEY = "invoice-processor-active-tab"' in home.text
    assert 'fetch(\n                  "/api/invoices?limit=500",\n                  { cache: "no-store" }' in home.text
    assert "function clearInvoicePreview()" in home.text
    assert "function connectLiveUpdates()" in home.text
    assert "new EventSource(" in home.text
    assert 'activityEventSource.addEventListener("invoice-update"' in home.text
    assert "window.setInterval(loadInvoices, 120000)" in home.text
    assert "metricsRefreshTimer = window.setTimeout(loadMetrics, 3000)" in home.text
    assert (
        'id="admin-companies-config"' in home.text
        and "loadSharePointFolderOptions();" in home.text
    )
    assert "function isFlaggedInvoice(invoice)" in home.text
    assert 'return invoice.status === "Needs Review";' in home.text
    assert "function invoiceDescription(invoice)" in home.text
    assert "Flagged:" in home.text
    assert "mini-description" in home.text
    assert "let openedFlaggedInvoiceId = null;" in home.text
    assert "openedFlaggedInvoiceId = id;" in home.text
    assert "const invoiceId = displayedInvoiceId;" in home.text
    assert 'document.addEventListener("visibilitychange"' in home.text
    assert "window.clearInterval(invoicePollTimer)" in home.text
    assert "let renderedInvoiceSnapshot = null;" in home.text
    assert "if (refreshedSnapshot === renderedInvoiceSnapshot) return;" in home.text
    assert "function activateTab(tab)" in home.text
    assert 'window.addEventListener("beforeunload"' in home.text
    assert "/api/invoice-search/filter?${params}" in home.text
    assert "Purchase Ledger: confirm invoice" in home.text
    assert 'id="delete-invoice-button" style="display:none">Delete duplicate' in home.text
    assert 'sendJson(`/api/invoices/${invoice.id}`, "DELETE")' in home.text
    assert "Manually add an invoice to Incoming Invoices" in home.text
    assert "Confirmation and automatic routing" not in home.text
    assert "Purchase Order number detected" not in home.text
    assert "Simulate incoming email" not in home.text


def test_simulated_email_endpoint_does_not_exist(tmp_path) -> None:
    client = client_for(tmp_path)

    response = client.post("/api/prototype/email-intake")

    assert response.status_code == 404


def test_health_identifies_outlook_intake_mode(tmp_path) -> None:
    client = client_for(tmp_path)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "outlook-intake"}


def test_supplier_list_is_filtered_by_selected_company(tmp_path) -> None:
    client = client_for(tmp_path)
    login = client.post(
        "/api/auth/login",
        json={"username": "purchase.ledger", "password": "ChangeMe-PL1!"},
    )
    assert login.status_code == 200

    response = client.get(
        "/api/suppliers",
        params={"company": "Northfield Manufacturing"},
    )

    assert response.status_code == 200
    assert [supplier["name"] for supplier in response.json()] == ["Supplier Ltd"]


def test_invoice_search_finds_normalized_irj_number(tmp_path) -> None:
    client = client_for(tmp_path)
    store = client.app.state.invoice_store
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(VALID_PDF_BYTES)
    invoice = store.add_from_outlook(
        message={"id": "message-1"},
        attachment={"id": "attachment-1", "name": "invoice.pdf"},
        stored_path=pdf,
    )
    store.update_fields(
        invoice.id,
        irj_number="000123",
        company="Acme Trading Ltd",
        status="Approved",
    )
    client.app.state.activity_feed.add_event(
        event_type="approved",
        target_role="purchase_ledger",
        message="Invoice 000123 approved and ready for payment.",
        invoice_id=invoice.id,
    )
    assert client.post(
        "/api/auth/login",
        json={"username": "purchase.ledger", "password": "ChangeMe-PL1!"},
    ).status_code == 200

    response = client.get("/api/invoice-search", params={"irj_number": "000123"})

    assert response.status_code == 200
    assert response.json()["id"] == invoice.id
    assert response.json()["irj_number"] == "000123"
    assert response.json()["status"] == "Approved"
    assert response.json()["audit_trail"][0]["event_type"] == "approved"
    assert "ready for payment" in response.json()["audit_trail"][0]["message"]


def test_invoice_search_returns_not_found_for_unknown_irj(tmp_path) -> None:
    client = client_for(tmp_path)
    assert client.post(
        "/api/auth/login",
        json={"username": "purchase.ledger", "password": "ChangeMe-PL1!"},
    ).status_code == 200

    response = client.get(
        "/api/invoice-search", params={"irj_number": "999999"}
    )

    assert response.status_code == 404


def test_invoice_search_rejects_non_six_digit_reference(tmp_path) -> None:
    client = client_for(tmp_path)
    assert client.post(
        "/api/auth/login",
        json={"username": "purchase.ledger", "password": "ChangeMe-PL1!"},
    ).status_code == 200

    response = client.get(
        "/api/invoice-search", params={"irj_number": "IRJ-000123"}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "An IRJ number must contain exactly six digits."
    )


def test_invoice_search_filters_by_company_and_supplier(tmp_path) -> None:
    client = client_for(tmp_path)
    store = client.app.state.invoice_store
    first = store.add_from_outlook(
        message={"id": "filter-message-1"},
        attachment={"id": "filter-attachment-1", "name": "first.pdf"},
        stored_path=tmp_path / "first.pdf",
    )
    second = store.add_from_outlook(
        message={"id": "filter-message-2"},
        attachment={"id": "filter-attachment-2", "name": "second.pdf"},
        stored_path=tmp_path / "second.pdf",
    )
    store.update_fields(
        first.id, company="Acme Trading Ltd", supplier="Supplier Ltd"
    )
    store.update_fields(
        second.id, company="Another Company", supplier="Other Supplier"
    )
    assert client.post(
        "/api/auth/login",
        json={"username": "purchase.ledger", "password": "ChangeMe-PL1!"},
    ).status_code == 200

    response = client.get(
        "/api/invoice-search/filter",
        params={"company": "Acme Trading Ltd", "supplier": "Supplier Ltd"},
    )

    assert response.status_code == 200
    assert [invoice["id"] for invoice in response.json()] == [first.id]


def test_statement_library_is_read_from_sharepoint(tmp_path) -> None:
    class FakeStatementClient:
        def list_statement_library(self):
            return {
                "Acme Trading Ltd": [
                    {
                        "id": "statement-1",
                        "name": "September.pdf",
                        "size": len(VALID_PDF_BYTES),
                        "webUrl": "https://sharepoint.example/statement-1",
                        "createdDateTime": "2026-09-01T08:00:00Z",
                        "lastModifiedDateTime": "2026-09-01T08:00:00Z",
                    }
                ]
            }

        def download_item(self, item_id: str) -> bytes:
            assert item_id == "statement-1"
            return VALID_PDF_BYTES

    client = TestClient(
        create_app(
            invoice_store=InvoiceStore(tmp_path / "invoices.db"),
            auth_store=AuthStore(tmp_path / "auth.db"),
            companies_store=CompanyStore(tmp_path / "config.db"),
            suppliers_store=SupplierStore(tmp_path / "config.db"),
            approval_matrix_store=ApprovalMatrixStore(tmp_path / "config.db"),
            activity_feed=ActivityFeedStore(tmp_path / "activity.db"),
            notification_store=OutlookNotificationStore(
                tmp_path / "notifications.db"
            ),
            sharepoint_client=cast(SharePointClient, FakeStatementClient()),
        )
    )
    assert client.post(
        "/api/auth/login",
        json={"username": "purchase.ledger", "password": "ChangeMe-PL1!"},
    ).status_code == 200

    listing = client.get("/api/statements")
    pdf = client.get("/api/statements/statement-1/pdf")

    assert listing.status_code == 200
    assert listing.json()["Acme Trading Ltd"][0]["name"] == "September.pdf"
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"


def test_confirmation_api_returns_po_routing_decision(tmp_path) -> None:
    client = client_for(tmp_path)

    response = client.post(
        "/api/workflow/confirm",
        json={
            "invoice_id": "invoice-123",
            "company": "Example Company",
            "company_folder": "/Companies/Example/Invoices",
            "original_filename": "invoice.pdf",
            "irj_number": "001245",
            "purchase_order_number": "PO-7788",
            "po_matching_folder": "/Companies/Example/PO Matching",
            "purchase_ledger_recipient": "purchase-ledger@example.test",
        },
    )

    assert response.status_code == 200
    assert response.json()["route"] == "purchase_order"
    assert response.json()["status"] == "Awaiting PO Matching"


def test_confirmation_api_returns_nominal_routing_decision(tmp_path) -> None:
    client = client_for(tmp_path)

    response = client.post(
        "/api/workflow/confirm",
        json={
            "invoice_id": "invoice-123",
            "company": "Example Company",
            "company_folder": "/Companies/Example/Invoices",
            "original_filename": "invoice.pdf",
            "irj_number": "001245",
        },
    )

    assert response.status_code == 200
    assert response.json()["route"] == "nominal"
    assert response.json()["destination_filename"] == "001245_invoice.pdf"
