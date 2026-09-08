"""Covers app/auth.py, role-guarded endpoints in app/main.py, admin
configuration CRUD, duplicate detection, and the approval on-hold/resume
flow -- the behaviours added to implement MANUAL_VS_AUTOMATED.md end to end."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.activity_feed import ActivityFeedStore
from app.ai_extraction import ExtractionResult
from app.approval_matrix import ApprovalMatrixStore
from app.auth import AuthStore
from app.companies import CompanyStore
from app.invoices import InvoiceStore
from app.main import create_app
from app.outlook_notifications import OutlookNotificationStore
from app.suppliers import SupplierStore


def make_client(tmp_path: Path) -> TestClient:
    # Every store must be pointed at tmp_path -- create_app() otherwise
    # falls back to the real runtime_data/*.db files, which would let test
    # runs silently write fabricated invoices/IRJ numbers/activity events
    # into whatever the live dev server is using.
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


def login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text


def test_manual_upload_runs_extraction_when_azure_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.main.ai_extraction_configured", lambda: True)
    client = make_client(tmp_path)
    client.app.state.lifecycle.extraction_runner = lambda path: ExtractionResult(
        company="Acme Trading Ltd",
        supplier="Supplier Ltd",
        supplier_invoice_number="INV-AUTO-1",
        purchase_order_number=None,
        invoice_date="2026-09-08",
        invoice_value=250.0,
        currency="GBP",
        confidence=0.96,
        needs_review=False,
        field_confidences={
            "company": 0.99,
            "supplier": 0.98,
            "supplier_invoice_number": 0.97,
            "invoice_date": 0.96,
            "invoice_value": 0.99,
            "currency": 0.99,
        },
        warnings=(),
    )
    login(client, "purchase.ledger", "ChangeMe-PL1!")

    response = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("auto.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )

    assert response.status_code == 200
    assert response.json()["supplier_invoice_number"] == "INV-AUTO-1"
    assert response.json()["ai_confidence"] == 0.96
    assert response.json()["status"] == "Needs Review"


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


def test_flagged_invoice_can_be_accepted_back_to_its_workflow_stage(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("review.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    invoice_id = upload.json()["id"]

    flagged = client.post(
        f"/api/invoices/{invoice_id}/flag-review",
        json={"reason": "Supplier identity needs checking."},
    )
    assert flagged.status_code == 200
    assert flagged.json()["status"] == "Needs Review"
    assert flagged.json()["review_return_status"] == "Awaiting AI Extraction"

    accepted = client.post(
        f"/api/invoices/{invoice_id}/review-decision",
        json={"accepted": True},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "Awaiting AI Extraction"
    assert accepted.json()["review_reason"] is None
    assert accepted.json()["review_return_status"] is None


def test_flagged_invoice_can_be_rejected_during_review(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("reject-review.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    invoice_id = upload.json()["id"]
    client.post(
        f"/api/invoices/{invoice_id}/flag-review",
        json={"reason": "Invoice appears invalid."},
    )

    rejected = client.post(
        f"/api/invoices/{invoice_id}/review-decision",
        json={"accepted": False, "reason": "Supplier confirmed it was issued in error."},
    )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "Rejected"
    assert rejected.json()["rejection_reason"] == (
        "Supplier confirmed it was issued in error."
    )


def test_duplicate_review_cannot_use_generic_acceptance(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    _upload_and_confirm(client, supplier_invoice_number="DEDICATED-DUP")
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("duplicate-review.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    invoice_id = upload.json()["id"]
    client.post(
        f"/api/invoices/{invoice_id}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": "DEDICATED-DUP",
        },
    )

    response = client.post(
        f"/api/invoices/{invoice_id}/review-decision",
        json={"accepted": True},
    )

    assert response.status_code == 422


def test_approver1_cannot_decide_level_2(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client, "jordan.blake", "ChangeMe-App1!")
    response = client.post(
        "/api/invoices/1/approve", json={"level": 2, "decision": "approved"}
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# End-to-end nominal invoice flow: confirm -> Sage registration -> approver1
# hold -> resume -> approver1 approve -> approver2 approve -> pay
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
    assert confirm.json()["status"] == "Awaiting Sage Registration"
    register = client.post(
        f"/api/invoices/{invoice_id}/register-sage",
        json={"sage_reference": f"SAGE-{supplier_invoice_number}"},
    )
    assert register.status_code == 200, register.text
    assert register.json()["status"] == "Awaiting Approval 1"
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
    client.app.state.invoice_store.update_fields(
        invoice_id,
        foreign_allocation_date="2026-01-14",
        foreign_allocation_reference="STALE-FX",
        foreign_allocated_by="Previous User",
    )
    paid = client.post(
        f"/api/invoices/{invoice_id}/pay",
        json={
            "payment_date": "2026-01-15",
            "supplier_account_number": "SUPP0001",
            "payment_reference": "BACS-001",
            "payment_method": "BACS",
        },
    )
    assert paid.status_code == 200
    assert paid.json()["status"] == "Paid / Awaiting Bank Reconciliation"
    assert paid.json()["is_foreign_payment"] == 0
    assert paid.json()["supplier_account_number"] == "SUPP0001"
    assert paid.json()["payment_route_decided_by"] == "Purchase Ledger"
    assert paid.json()["foreign_allocation_date"] is None
    assert paid.json()["foreign_allocation_reference"] is None
    assert paid.json()["foreign_allocated_by"] is None

    reconciled = client.post(
        f"/api/invoices/{invoice_id}/reconcile",
        json={
            "reconciliation_date": "2026-01-17",
            "notes": "Matched to bank statement.",
        },
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "Reconciled / Complete"


def test_foreign_payment_is_allocated_directly_to_complete(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    invoice_id = _upload_and_confirm(
        client, supplier_invoice_number="FOREIGN-1"
    )

    login(client, "jordan.blake", "ChangeMe-App1!")
    assert client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"level": 1, "decision": "approved"},
    ).status_code == 200
    login(client, "sam.ellis", "ChangeMe-App2!")
    approved = client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"level": 2, "decision": "approved"},
    )
    assert approved.json()["status"] == "Approved"

    login(client, "purchase.ledger", "ChangeMe-PL1!")
    client.app.state.invoice_store.update_fields(
        invoice_id,
        payment_date="2026-01-19",
        payment_reference="STALE-BACS",
        payment_method="BACS",
        paid_by="Previous User",
        reconciliation_date="2026-01-20",
        reconciliation_notes="Stale reconciliation",
        reconciled_by="Previous User",
    )
    routed = client.post(f"/api/invoices/{invoice_id}/route-foreign-payment")
    assert routed.status_code == 200
    assert routed.json()["status"] == "Foreign Payment / Awaiting Allocation"
    assert routed.json()["is_foreign_payment"] == 1
    assert routed.json()["payment_route_decided_by"] == "Purchase Ledger"
    assert routed.json()["payment_date"] is None
    assert routed.json()["payment_reference"] is None
    assert routed.json()["reconciliation_date"] is None

    reverted = client.post(
        f"/api/invoices/{invoice_id}/revert-foreign-payment"
    )
    assert reverted.status_code == 200
    assert reverted.json()["status"] == "Approved"
    assert reverted.json()["is_foreign_payment"] is None

    routed = client.post(f"/api/invoices/{invoice_id}/route-foreign-payment")
    assert routed.json()["status"] == "Foreign Payment / Awaiting Allocation"

    allocated = client.post(
        f"/api/invoices/{invoice_id}/allocate-foreign-payment",
        json={
            "allocation_date": "2026-01-20",
            "allocation_reference": "FX-ALLOC-9",
        },
    )
    assert allocated.status_code == 200
    assert allocated.json()["status"] == "Reconciled / Complete"
    assert allocated.json()["foreign_allocation_reference"] == "FX-ALLOC-9"
    assert allocated.json()["reconciliation_date"] is None


def test_on_hold_requires_a_comment(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    invoice_id = _upload_and_confirm(client, supplier_invoice_number="INV-2")

    login(client, "jordan.blake", "ChangeMe-App1!")
    response = client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"level": 1, "decision": "on_hold"},
    )
    assert response.status_code == 422


def test_missing_approver_route_can_be_configured_and_retried(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("new-supplier.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    invoice_id = upload.json()["id"]
    confirmed = client.post(
        f"/api/invoices/{invoice_id}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "New Route Supplier",
            "supplier_invoice_number": "NEW-ROUTE-1",
        },
    )
    assert confirmed.json()["status"] == "Awaiting Sage Registration"

    registered = client.post(
        f"/api/invoices/{invoice_id}/register-sage",
        json={"sage_reference": "SAGE-NEW-1"},
    )
    assert registered.json()["status"] == "Needs Review"
    assert "No approval matrix entry" in registered.json()["review_reason"]

    login(client, "admin", "ChangeMe-Admin1!")
    matrix = client.post(
        "/api/admin/approval-matrix",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "New Route Supplier",
            "approver1_name": "Approver One",
            "approver1_email": "approver.one@example.test",
        },
    )
    assert matrix.status_code == 200

    login(client, "purchase.ledger", "ChangeMe-PL1!")
    retried = client.post(
        f"/api/invoices/{invoice_id}/register-sage",
        json={"sage_reference": "SAGE-NEW-1"},
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "Awaiting Approval 1"
    assert retried.json()["sage_reference"] == "SAGE-NEW-1"


def test_po_query_resolution_requires_sage_registration_before_approval(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("po-invoice.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    invoice_id = upload.json()["id"]
    confirmed = client.post(
        f"/api/invoices/{invoice_id}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": "PO-INV-1",
            "purchase_order_number": "PO-100",
            "invoice_value": 750.0,
        },
    )
    assert confirmed.json()["status"] == "Awaiting PO Matching"

    query = client.post(
        f"/api/invoices/{invoice_id}/po-match",
        json={
            "matched": False,
            "notes": "Goods receipt is missing.",
            "query_category": "missing goods receipt",
            "purchasing_contact": "Purchasing Team",
        },
    )
    assert query.json()["status"] == "PO Query / Matching Issue"

    matched = client.post(
        f"/api/invoices/{invoice_id}/po-match",
        json={"matched": True, "notes": "Goods receipt recorded."},
    )
    assert matched.json()["status"] == "Awaiting Sage Registration"

    registered = client.post(
        f"/api/invoices/{invoice_id}/register-sage",
        json={"sage_reference": "SAGE-PO-100"},
    )
    assert registered.status_code == 200
    assert registered.json()["status"] == "Approved"
    assert registered.json()["sage_registered_by"] == "Purchase Ledger"


def test_po_invoice_can_be_rejected_from_matching(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("po-reject.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    invoice_id = upload.json()["id"]
    client.post(
        f"/api/invoices/{invoice_id}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": "PO-REJECT",
            "purchase_order_number": "PO-REJECT",
        },
    )

    rejected = client.post(
        f"/api/invoices/{invoice_id}/reject",
        json={"reason": "Invoice is not valid for this company."},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "Rejected"
    assert rejected.json()["rejection_reason"] == (
        "Invoice is not valid for this company."
    )


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
    assert override.json()["status"] == "Awaiting Sage Registration"
    assert override.json()["duplicate_of_invoice_id"] is None


def test_confirmed_duplicate_can_be_cancelled(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    _upload_and_confirm(client, supplier_invoice_number="DUP-CANCEL")

    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("duplicate.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    invoice_id = upload.json()["id"]
    duplicate = client.post(
        f"/api/invoices/{invoice_id}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": "DUP-CANCEL",
            "invoice_value": 500.0,
        },
    )
    assert duplicate.json()["status"] == "Needs Review"

    cancelled = client.post(f"/api/invoices/{invoice_id}/cancel-duplicate")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "Cancelled - Duplicate"
    assert cancelled.json()["cancelled_by"] == "Purchase Ledger"


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
    assert confirm1.json()["status"] == "Awaiting Sage Registration"

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
