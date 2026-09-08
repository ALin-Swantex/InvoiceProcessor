import io
import sqlite3

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.activity_feed import ActivityFeedStore
from app.approval_matrix import ALL_COMPANIES, ApprovalMatrixStore
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
    assert summary.warning_count == 1
    assert summary.skipped_count == 0
    assert summary.rows[1].status == "warning"
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

    # Supplier/payment data remains useful even when the route needs users.
    assert supplier_store.get("Unresolvable Supplier") is not None
    assert (
        supplier_terms_store.get("Brand New Co", "Unresolvable Supplier")
        is not None
    )


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
    payment_defaults = client.get(
        "/api/supplier-terms",
        params={"company": "Acme Trading Ltd", "supplier": "Supplier Ltd"},
    )
    assert payment_defaults.status_code == 200
    assert payment_defaults.json()["profiles"][0]["default_payment_method"] == "Cheque"


def test_imports_attached_workbook_shape_for_selected_company(tmp_path) -> None:
    company_store = CompanyStore(tmp_path / "config.db")
    supplier_store = SupplierStore(tmp_path / "config.db")
    approval_matrix_store = ApprovalMatrixStore(tmp_path / "config.db")
    supplier_terms_store = SupplierTermsStore(tmp_path / "config.db")
    auth_store = AuthStore(tmp_path / "auth.db")

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "index",
            "Supplier Account No",
            "Trading Partner Name",
            "DefaultPaymentMethod",
            "PaymentTerms",
            "Pay from bank account",
            "1st approval",
            "Final Signature",
        ]
    )
    sheet.append(
        [
            0,
            "ADOB0001",
            "Adobe Systems Software",
            "Direct Debit",
            "7 Days",
            "Nat West - Onecard",
            "Mark Kelly",
            "Graham Rogers",
        ]
    )
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    summary = import_supplier_workbook(
        buffer,
        company_store=company_store,
        supplier_store=supplier_store,
        approval_matrix_store=approval_matrix_store,
        supplier_terms_store=supplier_terms_store,
        auth_store=auth_store,
    )

    assert summary.warning_count == 1
    terms = supplier_terms_store.get(
        "Any Internal Company", "Adobe Systems Software"
    )
    assert terms is not None
    assert terms.company == ALL_COMPANIES
    assert terms.supplier_account_number == "ADOB0001"
    assert terms.default_payment_method == "Direct Debit"
    assert terms.payment_terms_notice == "7 Days"
    assert terms.bank_account == "Nat West - Onecard"
    assert "Mark Kelly" in summary.rows[0].reason


def test_global_approval_route_is_used_for_any_invoice_company(tmp_path) -> None:
    store = ApprovalMatrixStore(tmp_path / "config.db")
    store.create(
        company=ALL_COMPANIES,
        supplier="Adobe Systems Software",
        approver1_name="Mark Kelly",
        approver1_email="mark@example.test",
        approver2_name="Graham Rogers",
        approver2_email="graham@example.test",
    )

    route = store.find("Any Internal Company", "Adobe Systems Software")

    assert route is not None
    assert route.approver1.name == "Mark Kelly"
    assert route.approver2 is not None
    assert route.approver2.name == "Graham Rogers"


def test_workbook_without_company_imports_global_supplier_profile(tmp_path) -> None:
    workbook = Workbook()
    workbook.active.append(["Trading Partner Name"])
    workbook.active.append(["Adobe Systems Software"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    supplier_store = SupplierStore(tmp_path / "config.db")
    terms_store = SupplierTermsStore(tmp_path / "config.db")
    summary = import_supplier_workbook(
        buffer,
        company_store=CompanyStore(tmp_path / "config.db"),
        supplier_store=supplier_store,
        approval_matrix_store=ApprovalMatrixStore(tmp_path / "config.db"),
        supplier_terms_store=terms_store,
        auth_store=AuthStore(tmp_path / "auth.db"),
    )

    assert summary.warning_count == 1
    assert supplier_store.get("Adobe Systems Software") is not None
    profile = terms_store.list_for_supplier(
        "Any Internal Company", "Adobe Systems Software"
    )[0]
    assert profile.company == ALL_COMPANIES


def test_supplier_terms_store_migrates_legacy_company_supplier_key(tmp_path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE supplier_terms (
                company TEXT NOT NULL,
                supplier TEXT NOT NULL,
                supplier_account_number TEXT,
                default_payment_method TEXT,
                payment_terms_notice TEXT,
                bank_account TEXT,
                PRIMARY KEY (company, supplier)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO supplier_terms VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "Acme Trading Ltd",
                "Supreme Freight Services Ltd",
                "SUPR0001",
                "BACS",
                "60 Days EOM",
                "Nat West - GBP",
            ),
        )

    store = SupplierTermsStore(database)
    store.upsert(
        company="Acme Trading Ltd",
        supplier="Supreme Freight Services Ltd",
        supplier_account_number="SUPR0002",
        default_payment_method="BACS",
        payment_terms_notice="60 Days EOM",
        bank_account="Nat West - USD",
    )

    profiles = store.list_for_supplier(
        "Acme Trading Ltd", "Supreme Freight Services Ltd"
    )
    assert [profile.supplier_account_number for profile in profiles] == [
        "SUPR0001",
        "SUPR0002",
    ]


def test_deleting_supplier_cascades_payment_profiles_and_routes(tmp_path) -> None:
    client = client_for(tmp_path)
    login(client, "admin", "ChangeMe-Admin1!")
    client.post("/api/admin/suppliers", json={"name": "Delete Me Ltd"})
    client.post(
        "/api/admin/approval-matrix",
        json={
            "company": "*",
            "supplier": "Delete Me Ltd",
            "approver1_name": "Jordan Blake",
            "approver1_email": "jordan@example.test",
        },
    )
    client.app.state.supplier_terms_store.upsert(
        company="*",
        supplier="Delete Me Ltd",
        supplier_account_number="DEL0001",
        default_payment_method="BACS",
    )

    response = client.delete("/api/admin/suppliers/Delete%20Me%20Ltd")

    assert response.status_code == 200
    assert client.app.state.approval_matrix_store.find(
        "Acme Trading Ltd", "Delete Me Ltd"
    ) is None
    assert client.app.state.supplier_terms_store.list_for_supplier(
        "Acme Trading Ltd", "Delete Me Ltd"
    ) == []


def test_deleting_internal_company_preserves_global_supplier_settings(tmp_path) -> None:
    client = client_for(tmp_path)
    login(client, "admin", "ChangeMe-Admin1!")
    client.post(
        "/api/admin/companies",
        json={
            "name": "Delete Company Ltd",
            "company_folder": "Invoices/Delete Company Ltd",
            "po_matching_folder": "Invoices/Delete Company Ltd/PO Matching",
        },
    )
    terms_store = client.app.state.supplier_terms_store
    terms_store.upsert(
        company="Delete Company Ltd",
        supplier="Supplier Ltd",
        supplier_account_number="SPECIFIC",
    )
    terms_store.upsert(
        company="*",
        supplier="Supplier Ltd",
        supplier_account_number="GLOBAL",
    )

    response = client.delete("/api/admin/companies/Delete%20Company%20Ltd")

    assert response.status_code == 200
    profiles = terms_store.list_for_supplier(
        "Delete Company Ltd", "Supplier Ltd"
    )
    assert [profile.supplier_account_number for profile in profiles] == ["GLOBAL"]
