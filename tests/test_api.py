from fastapi.testclient import TestClient

from app.activity_feed import ActivityFeedStore
from app.approval_matrix import ApprovalMatrixStore
from app.auth import AuthStore
from app.companies import CompanyStore
from app.invoices import InvoiceStore
from app.main import create_app
from app.outlook_notifications import OutlookNotificationStore
from app.suppliers import SupplierStore


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
    assert "OUTLOOK INTAKE CONNECTED - AI NOT CONNECTED" in home.text
    assert "Invoice PDF" in home.text
    assert "IRJ number" in home.text
    assert "Company being invoiced" in home.text
    assert "Supplier invoice number" in home.text
    assert "Purchase Order number" in home.text
    assert "Invoice value" in home.text
    assert "Overall confidence" in home.text
    assert "Purchase Ledger: confirm invoice" in home.text
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


def test_confirmation_api_returns_po_routing_decision(tmp_path) -> None:
    client = client_for(tmp_path)

    response = client.post(
        "/api/workflow/confirm",
        json={
            "invoice_id": "invoice-123",
            "company": "Example Company",
            "company_folder": "/Companies/Example/Invoices",
            "original_filename": "invoice.pdf",
            "irj_number": "IRJ-001245",
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
            "irj_number": "IRJ-001245",
        },
    )

    assert response.status_code == 200
    assert response.json()["route"] == "nominal"
    assert response.json()["destination_filename"] == "IRJ-001245_invoice.pdf"
