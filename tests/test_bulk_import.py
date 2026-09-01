import io

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.activity_feed import ActivityFeedStore
from app.approval_matrix import ApprovalMatrixStore
from app.auth import AuthStore
from app.bulk_import import import_supplier_workbook
from app.companies import CompanyStore
from app.invoices import InvoiceStore
from app.main import create_app
from app.outlook_notifications import OutlookNotificationStore
from app.suppliers import SupplierStore
from app.supplier_terms import SupplierTermsStore


def client_for(tmp_path) -> TestClient:
    return TestClient(
        create_app(
            invoice_store=InvoiceStore(tmp_path / "invoices.db"),
            auth_store=AuthStore(tmp_path / "auth.db"),
            companies_store=CompanyStore(tmp_path / "config.db"),
            suppliers_store=SupplierStore(tmp_path / "config.db"),
            approval_matrix_store=ApprovalMatrixStore(tmp_path / "config.db"),
            supplier_terms_store=SupplierTermsStore(tmp_path / "config.db"),
            activity_feed=ActivityFeedStore(tmp_path / "activity_feed.db"),
            notification_store=OutlookNotificationStore(
                tmp_path / "outlook_notifications.db"
            ),
        )
    )


def login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text


def make_workbook(rows: list[list[object]]) -> io.BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "Company",
            "Supplier",
            "Supplier Account Number",
            "Default Payment Method",
            "Payment Terms",
            "Bank Account",
            "Approver",
            "Approver 2",
        ]
    )
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def test_import_supplier_workbook_creates_master_data_and_terms(tmp_path) -> None:
    company_store = CompanyStore(tmp_path / "config.db")
    supplier_store = SupplierStore(tmp_path / "config.db")
    approval_matrix_store = ApprovalMatrixStore(tmp_path / "config.db")
    supplier_terms_store = SupplierTermsStore(tmp_path / "config.db")
    auth_store = AuthStore(tmp_path / "auth.db")

    workbook = make_workbook(
        [
            [
                "Brand New Co",
                "Brand New Supplier",
                "ACC-123",
                "BACS",
                "30 days net",
                "GBP Main Account",
                "Jordan Blake (Approver 1)",
                "Sam Ellis (Approver 2)",
            ],
            [
                "Brand New Co",
                "Unresolvable Supplier",
                "ACC-999",
                "BACS",
                "60 days net",
                "GBP Main Account",
                "Nobody Real",
                None,
            ],
        ]
    )

    summary = import_supplier_workbook(
        workbook,
        company_store=company_store,
        supplier_store=supplier_store,
        approval_matrix_store=approval_matrix_store,
        supplier_terms_store=supplier_terms_store,
        auth_store=auth_store,
    )

    assert summary.imported_count == 1
    assert summary.skipped_count == 1
    assert summary.rows[1].status == "skipped"
    assert "Nobody Real" in summary.rows[1].reason

    assert company_store.get("Brand New Co") is not None
    assert supplier_store.get("Brand New Supplier") is not None

    matrix_entry = approval_matrix_store.find("Brand New Co", "Brand New Supplier")
    assert matrix_entry is not None
    assert matrix_entry.approver1.name == "Jordan Blake (Approver 1)"
    assert matrix_entry.approver2.name == "Sam Ellis (Approver 2)"

    terms = supplier_terms_store.get("Brand New Co", "Brand New Supplier")
    assert terms is not None
    assert terms.supplier_account_number == "ACC-123"
    assert terms.default_payment_method == "BACS"
    assert terms.payment_terms_notice == "30 days net"
    assert terms.bank_account == "GBP Main Account"

    # The unresolvable row must not have created a company/supplier orphan.
    assert supplier_store.get("Unresolvable Supplier") is None


def test_admin_import_endpoint_requires_admin_role(tmp_path) -> None:
    client = client_for(tmp_path)
    login(client, "jordan.blake", "ChangeMe-App1!")

    workbook = make_workbook([])
    response = client.post(
        "/api/admin/import/supplier-master-data",
        files={
            "file": (
                "suppliers.xlsx",
                workbook.read(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 403


def test_admin_import_endpoint_imports_rows_end_to_end(tmp_path) -> None:
    client = client_for(tmp_path)
    login(client, "admin", "ChangeMe-Admin1!")

    workbook = make_workbook(
        [
            [
                "Acme Trading Ltd",
                "Supplier Ltd",
                "ACC-001",
                "Cheque",
                "14 days net",
                "GBP Acme Account",
                "Jordan Blake (Approver 1)",
                None,
            ]
        ]
    )
    response = client.post(
        "/api/admin/import/supplier-master-data",
        files={
            "file": (
                "suppliers.xlsx",
                workbook.read(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] == 1
    assert body["skipped"] == 0

    terms_response = client.get("/api/admin/supplier-terms")
    assert terms_response.status_code == 200
    terms = terms_response.json()
    assert any(
        t["supplier"] == "Supplier Ltd" and t["supplier_account_number"] == "ACC-001"
        for t in terms
    )
