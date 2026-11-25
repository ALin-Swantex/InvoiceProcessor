import re
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


def interface_text(client: TestClient) -> str:
    """The page plus every stylesheet and script it loads, with runs of
    whitespace collapsed so indentation and file boundaries do not matter."""
    page = client.get("/")
    assert page.status_code == 200
    assets = re.findall(
        r'<(?:link rel="stylesheet" href|script src)="(/static/[^"]+)"', page.text
    )
    assert any(asset.endswith(".js") for asset in assets)
    parts = [page.text]
    for asset in assets:
        response = client.get(asset)
        assert response.status_code == 200, asset
        parts.append(response.text)
    return " ".join("\n".join(parts).split())


def test_invoice_review_interface_contains_required_sections(tmp_path) -> None:
    client = client_for(tmp_path)
    text = interface_text(client)

    def has(snippet: str) -> bool:
        return " ".join(snippet.split()) in text

    assert has('<link rel="stylesheet" href="/static/app.css">')
    assert has("<h1>Swantex</h1>")
    assert has("Continue with Microsoft 365")
    assert has("Protected by Microsoft Entra ID")
    assert not has("OUTLOOK INTAKE CONNECTED - AI NOT CONNECTED")
    assert has('<aside class="sidebar">')
    assert has('data-tab="needs-review"')
    assert text.index('data-tab="search"') < text.index('data-tab="incoming"')
    assert text.index('data-tab="statements"') < text.index('data-tab="incoming"')
    assert has('data-tab="statements"')
    assert has("Flagged Documents — Purchase Ledger Review")
    assert has('id="payment-method"')
    assert has('<select id="admin-import-company" required>')
    assert has("Supplier payment settings")
    assert not has("<h3>Users</h3>")
    assert not has("<h3>Microsoft 365 roles</h3>")
    assert not has('id="admin-user-form"')
    assert has('id="admin-bi-metrics"')
    assert has('id="metrics-granularity"')
    assert has("Invoice volume interval")
    assert has("Total pending invoice value by company")
    assert has("Spend by supplier and company")
    assert has("function loadMetrics()")
    assert has("async function readResponseBody(response)")
    assert has('<details class="admin-block admin-collapsible">')
    assert has("<summary>Companies</summary>")
    assert has('id="admin-company-root-folder" required disabled')
    assert has('<select id="admin-supplier-default-company">')
    assert has('id="admin-supplier-invoice-pattern"')
    assert has("# = digit, @ = letter, * = letter or digit")
    assert has('<select id="admin-matrix-company" required>')
    assert has('<select id="admin-matrix-supplier" required>')
    assert has("All invoice companies")
    assert has('id="admin-edit-dialog"')
    assert has("function openAdminEditor")
    assert has('data-edit-index="${index}"')
    assert has('${escapeHtml(c.value(row) ?? "—")}')
    assert has('data-delete-index="${index}"')
    assert has("${escapeHtml(r.reason)}</li>")
    assert has("supplier companies already exist")
    assert has('formData.set("replace_existing", "true")')
    assert has('.join("\\n")')
    assert has('id="admin-company-folder-preview"')
    assert has("Statement classification")
    assert has("IRJ number")
    assert has("Company being invoiced")
    assert has('<select id="preview-company">')
    assert has('<select id="preview-supplier" disabled>')
    assert not has("Purchase Ledger confirmation (editable")
    assert has("Invoice details (AI-extracted — review and correct before confirming)")
    assert not has("Outlook email and PDF data will be loaded here.")
    assert not has('id="intake-notice"')
    assert has("function renderCompanyGroupedTables")
    assert has("function suppliersGroupedByCompany")
    assert has("function refreshAdminMatrixSupplierOptions")
    assert has("/api/suppliers?company=${encodeURIComponent(company)}")
    assert has("Supplier invoice number")
    assert has("Purchase Order number")
    assert has("Invoice value")
    assert has("Overall confidence")
    assert has("PDF and extracted fields")
    assert has('${expanded ? "Hide" : "Show"} details')
    assert has("function invoiceAnalysisHtml")
    assert has("function invoiceHistoryHtml(invoice)")
    assert has("function invoiceAuditHtml(invoice)")
    assert has("function formatUkTimestamp(value)")
    assert has('timeZone: "Europe/London"')
    assert has('notes.join("\\n\\n")')
    assert has('${invoice.approver1_name || "Approval"} note')
    assert has('${invoice.approver2_name || "Approval"} note')
    assert has("Description and history")
    assert has("<h4>Audit trail</h4>")
    assert has("Approval query / on hold")
    assert has("Payment and reconciliation")
    assert has('id="supplier-match-prompt"')
    assert has("function suggestedSupplier(extractedSupplier, suppliers)")
    assert has("Is this")
    assert has("not registered for")
    assert has('data-expand-invoice="${invoice.id}"')
    assert has('class="row-action-buttons"')
    assert has('class="action-link"')
    assert has('button.setAttribute("aria-busy", "true")')
    assert has('button.textContent = "Working…"')
    assert has('class="invoice-analysis-pdf"')
    assert has("Extracted invoice analysis")
    assert has("File as statement")
    assert has('data-action="reject-flagged"')
    assert has("/api/invoices/${id}/reject-flagged")
    assert has("/api/invoices/${id}/file-statement")
    assert has("/api/invoices/${id}/mark-as-invoice")
    assert has("Search invoices")
    assert has('data-tab="payment-bacs"')
    assert has('data-tab="payment-bankline"')
    assert has('data-tab="payment-foreign-poa"')
    assert has("Appeared on bank statement")
    assert has("Marked reconciled")
    assert has("Uploaded '${record.original_filename}' to ${destination}.")
    assert has("Statements/{Company}")
    assert has('id="statement-company"')
    assert has('fetch("/api/statements")')
    assert has('const ACTIVE_TAB_STORAGE_KEY = "invoice-processor-active-tab"')
    assert has('fetch(\n                  "/api/invoices?limit=500",\n                  { cache: "no-store" }')
    assert has("function clearInvoicePreview()")
    assert has("function connectLiveUpdates()")
    assert has("new EventSource(")
    assert has('activityEventSource.addEventListener("invoice-update"')
    assert has("window.setInterval(loadInvoices, 120000)")
    assert has("metricsRefreshTimer = window.setTimeout(loadMetrics, 3000)")
    assert (
        has('id="admin-companies-config"')
        and has("loadSharePointFolderOptions();")
    )
    assert has("function isFlaggedInvoice(invoice)")
    assert has('return invoice.status === "Needs Review";')
    assert has("function invoiceDescription(invoice)")
    assert has("Flagged:")
    assert has("mini-description")
    assert has("let openedFlaggedInvoiceId = null;")
    assert has("openedFlaggedInvoiceId = id;")
    assert has("const invoiceId = displayedInvoiceId;")
    assert has('document.addEventListener("visibilitychange"')
    assert has("window.clearInterval(invoicePollTimer)")
    assert has("let renderedInvoiceSnapshot = null;")
    assert has("if (refreshedSnapshot === renderedInvoiceSnapshot) return;")
    assert has("function activateTab(tab)")
    assert has('window.addEventListener("beforeunload"')
    assert has("/api/invoice-search/filter?${params}")
    assert has("Purchase Ledger: confirm invoice")
    assert has('id="delete-invoice-button" style="display:none">Delete duplicate')
    assert has('sendJson(`/api/invoices/${invoice.id}`, "DELETE")')
    assert has("Manually add an invoice to Incoming Invoices")
    assert not has("Confirmation and automatic routing")
    assert not has("Purchase Order number detected")
    assert not has("Simulate incoming email")


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


def test_company_options_list_companies_with_a_route_for_the_supplier(tmp_path) -> None:
    client = client_for(tmp_path)
    store = client.app.state.invoice_store
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(VALID_PDF_BYTES)
    invoice = store.add_from_outlook(
        message={"id": "message-1"},
        attachment={"id": "attachment-1", "name": "invoice.pdf"},
        stored_path=pdf,
    )
    # The seed routes "Supplier Ltd" under Acme and Northfield, not Riverside.
    store.update_fields(
        invoice.id, company="Riverside Logistics", supplier="Supplier Ltd"
    )
    login = client.post(
        "/api/auth/login",
        json={"username": "purchase.ledger", "password": "ChangeMe-PL1!"},
    )
    assert login.status_code == 200

    response = client.get(f"/api/invoices/{invoice.id}/company-options")

    assert response.status_code == 200
    assert response.json() == {
        "supplier": "Supplier Ltd",
        "has_route": False,
        "companies": ["Acme Trading Ltd", "Northfield Manufacturing"],
    }

    store.update_fields(invoice.id, company="Acme Trading Ltd")
    response = client.get(f"/api/invoices/{invoice.id}/company-options")
    assert response.json()["has_route"] is True

    # A route for a supplier missing from the register offers no companies:
    # the review form could not select that supplier anyway.
    client.app.state.approval_matrix_store.create(
        company="Riverside Logistics",
        supplier="Unregistered Paper",
        approver1_name="Morgan Reyes",
        approver1_email="morgan.reyes@example.test",
    )
    store.update_fields(
        invoice.id, company="Acme Trading Ltd", supplier="Unregistered Paper"
    )
    response = client.get(f"/api/invoices/{invoice.id}/company-options")
    assert response.json()["companies"] == []


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


def _sign_in_purchase_ledger(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "purchase.ledger", "password": "ChangeMe-PL1!"},
    )
    assert response.status_code == 200


def test_confirmation_api_requires_sign_in(tmp_path) -> None:
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

    assert response.status_code == 401


def test_confirmation_api_returns_po_routing_decision(tmp_path) -> None:
    client = client_for(tmp_path)
    _sign_in_purchase_ledger(client)

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
    _sign_in_purchase_ledger(client)

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
