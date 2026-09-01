"""Covers app/auth.py, role-guarded endpoints in app/main.py, admin
configuration CRUD, duplicate detection, and the approval on-hold/resume
flow -- the behaviours added to implement MANUAL_VS_AUTOMATED.md end to end."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.approval_matrix import ApprovalMatrixStore
from app.auth import AuthStore
from app.companies import CompanyStore
from app.invoices import InvoiceStore
from app.main import create_app
from app.suppliers import SupplierStore


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            invoice_store=InvoiceStore(tmp_path / "invoices.db"),
            auth_store=AuthStore(tmp_path / "auth.db"),
            companies_store=CompanyStore(tmp_path / "config.db"),
            suppliers_store=SupplierStore(tmp_path / "config.db"),
            approval_matrix_store=ApprovalMatrixStore(tmp_path / "config.db"),
        )
    )


def login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_login_succeeds_with_seeded_credentials_and_sets_role(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "ChangeMe-Admin1!"},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "admin"

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "admin"


def test_login_fails_with_wrong_password(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": "wrong"}
    )
    assert response.status_code == 401


def test_me_requires_authentication(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.get("/api/auth/me")
    assert response.status_code == 401


def test_logout_clears_session(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client, "admin", "ChangeMe-Admin1!")
    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# Role guards
# ---------------------------------------------------------------------------


def test_non_admin_cannot_access_admin_endpoints(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    assert client.get("/api/admin/companies").status_code == 403
    assert client.get("/api/admin/users").status_code == 403


def test_admin_can_manage_companies_suppliers_and_matrix(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client, "admin", "ChangeMe-Admin1!")

    created = client.post(
        "/api/admin/companies",
        json={
            "name": "New Co Ltd",
            "company_folder": "Invoices/New Co Ltd",
            "po_matching_folder": "Invoices/New Co Ltd/PO Matching",
            "aliases": ["New Co"],
        },
    )
    assert created.status_code == 200, created.text
    assert any(c["name"] == "New Co Ltd" for c in client.get("/api/admin/companies").json())

    supplier = client.post(
        "/api/admin/suppliers",
        json={"name": "New Supplier", "contact_email": "ns@example.test"},
    )
    assert supplier.status_code == 200

    matrix_entry = client.post(
        "/api/admin/approval-matrix",
        json={
            "company": "New Co Ltd",
            "supplier": "New Supplier",
            "approver1_name": "Approver One",
            "approver1_email": "approver.one@example.test",
        },
    )
    assert matrix_entry.status_code == 200
    entry_id = matrix_entry.json()["id"]

    updated = client.put(
        f"/api/admin/approval-matrix/{entry_id}",
        json={"approver2_name": "Approver Two", "approver2_email": "approver.two@example.test"},
    )
    assert updated.status_code == 200
    assert updated.json()["approver2_email"] == "approver.two@example.test"

    deleted = client.delete(f"/api/admin/approval-matrix/{entry_id}")
    assert deleted.status_code == 200


def test_purchase_ledger_cannot_approve_invoice(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    response = client.post(
        "/api/invoices/1/approve", json={"level": 1, "decision": "approved"}
    )
    assert response.status_code == 403


def test_approver1_cannot_decide_level_2(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client, "jordan.blake", "ChangeMe-App1!")
    response = client.post(
        "/api/invoices/1/approve", json={"level": 2, "decision": "approved"}
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# End-to-end nominal invoice flow: confirm -> approver1 hold -> resume ->
# approver1 approve -> approver2 approve -> pay
# ---------------------------------------------------------------------------


def _upload_and_confirm(client: TestClient, *, supplier_invoice_number: str = "INV-1") -> int:
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("invoice.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    assert upload.status_code == 200, upload.text
    invoice_id = upload.json()["id"]

    confirm = client.post(
        f"/api/invoices/{invoice_id}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": supplier_invoice_number,
            "invoice_value": 500.0,
            "currency": "GBP",
        },
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["status"] == "Awaiting Approval 1"
    return invoice_id


def test_full_nominal_approval_hold_resume_and_pay_flow(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    invoice_id = _upload_and_confirm(client)

    # Approver 1 places the invoice on hold with a required comment.
    login(client, "jordan.blake", "ChangeMe-App1!")
    hold = client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"level": 1, "decision": "on_hold", "comments": "Querying unit price with supplier."},
    )
    assert hold.status_code == 200, hold.text
    assert hold.json()["status"] == "Approval Query / On Hold"

    # A hold requires a comment.
    login(client, "jordan.blake", "ChangeMe-App1!")

    # Purchase Ledger resumes once resolved.
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    resumed = client.post(
        f"/api/invoices/{invoice_id}/resume-approval",
        json={"resolution_notes": "Supplier confirmed the price is correct."},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "Awaiting Approval 1"

    # Approver 1 approves; since a second approver is configured this moves
    # to Awaiting Approval 2 rather than Approved.
    login(client, "jordan.blake", "ChangeMe-App1!")
    approve1 = client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"level": 1, "decision": "approved", "comments": "Looks correct."},
    )
    assert approve1.status_code == 200
    assert approve1.json()["status"] == "Awaiting Approval 2"

    # Approver 2 approves -> fully approved.
    login(client, "sam.ellis", "ChangeMe-App2!")
    approve2 = client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"level": 2, "decision": "approved"},
    )
    assert approve2.status_code == 200
    assert approve2.json()["status"] == "Approved"

    # Purchase Ledger marks it paid.
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    paid = client.post(
        f"/api/invoices/{invoice_id}/pay",
        json={
            "payment_date": "2026-01-15",
            "payment_reference": "BACS-001",
            "payment_method": "BACS",
        },
    )
    assert paid.status_code == 200
    assert paid.json()["status"] == "Paid / Awaiting Bank Reconciliation"


def test_on_hold_requires_a_comment(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    invoice_id = _upload_and_confirm(client, supplier_invoice_number="INV-2")

    login(client, "jordan.blake", "ChangeMe-App1!")
    response = client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"level": 1, "decision": "on_hold"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def test_duplicate_invoice_is_flagged_for_review(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    _upload_and_confirm(client, supplier_invoice_number="DUP-1")

    # A second invoice for the same company/supplier/supplier-invoice-number
    # combination should be flagged rather than routed.
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload2 = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("invoice2.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    invoice2_id = upload2.json()["id"]
    confirm2 = client.post(
        f"/api/invoices/{invoice2_id}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": "DUP-1",
            "invoice_value": 500.0,
        },
    )
    assert confirm2.status_code == 200
    body = confirm2.json()
    assert body["status"] == "Needs Review"
    assert body["duplicate_of_invoice_id"] is not None

    # Purchase Ledger can override once they've confirmed it's genuinely
    # a separate invoice.
    override = client.post(
        f"/api/invoices/{invoice2_id}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": "DUP-1",
            "invoice_value": 500.0,
            "override_duplicate": True,
        },
    )
    assert override.status_code == 200
    assert override.json()["status"] == "Awaiting Approval 1"


def test_duplicate_invoice_is_flagged_when_invoice_number_is_blank(
    tmp_path: Path,
) -> None:
    """If Purchase Ledger confirms an invoice without typing a supplier
    invoice number (e.g. AI extraction hasn't run and nobody filled it in
    manually), the primary company+supplier+number duplicate key has
    nothing to compare -- but re-processing the exact same source PDF
    should still be caught via the file-identity fallback rather than
    silently routing a second time."""
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")

    upload1 = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("same-invoice.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    invoice1_id = upload1.json()["id"]
    confirm1 = client.post(
        f"/api/invoices/{invoice1_id}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": None,
            "invoice_value": 500.0,
        },
    )
    assert confirm1.status_code == 200, confirm1.text
    assert confirm1.json()["status"] == "Awaiting Approval 1"

    # The exact same PDF (same filename + size) is added again and
    # confirmed the same way, again without a supplier invoice number.
    upload2 = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("same-invoice.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    invoice2_id = upload2.json()["id"]
    confirm2 = client.post(
        f"/api/invoices/{invoice2_id}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": None,
            "invoice_value": 500.0,
        },
    )
    assert confirm2.status_code == 200, confirm2.text
    body = confirm2.json()
    assert body["status"] == "Needs Review"
    assert body["duplicate_of_invoice_id"] == invoice1_id
    assert "matched on matching filename and file size" in body["review_reason"]
