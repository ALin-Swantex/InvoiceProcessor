"""Covers app/auth.py, role-guarded endpoints in app/main.py, admin
configuration CRUD, duplicate detection, and the approval on-hold/resume
flow -- the behaviours added to implement MANUAL_VS_AUTOMATED.md end to end."""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier, Lock

import pytest
from fastapi.testclient import TestClient

from app.activity_feed import ActivityFeedStore
from app.ai_extraction import ExtractionResult
from app.approval_matrix import ApprovalMatrixStore
from app.auth import AuthStore
from app.companies import CompanyStore
from app.config_db import SQLiteProcessConfigurationStore
from app.invoices import InvoiceStore
from app.invoice_lifecycle import InvoiceLifecycleError
from app.main import create_app
from app.outlook_notifications import OutlookNotificationStore
from app.suppliers import SupplierStore
from tests.pdf_helpers import VALID_PDF_BYTES


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
            process_configuration_store=SQLiteProcessConfigurationStore(
                tmp_path / "config.db"
            ),
            activity_feed=ActivityFeedStore(tmp_path / "activity_feed.db"),
            notification_store=OutlookNotificationStore(
                tmp_path / "outlook_notifications.db"
            ),
            auto_configure_sharepoint=False,
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
        files={"file": ("auto.pdf", VALID_PDF_BYTES, "application/pdf")},
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


def test_supplier_names_and_aliases_are_case_insensitive(tmp_path: Path) -> None:
    suppliers = SupplierStore(tmp_path / "config.db")
    profile = suppliers.create(
        name="Case Sensitive Supplies",
        aliases=["CSS Limited"],
    )

    assert suppliers.get("case sensitive supplies") == profile
    assert suppliers.get("css limited") == profile
    with pytest.raises(ValueError, match="regardless of capitalisation"):
        suppliers.create(name="CASE SENSITIVE SUPPLIES")
    with pytest.raises(ValueError, match="regardless of capitalisation"):
        suppliers.create(name="Another Supplier", aliases=["css LIMITED"])

    updated = suppliers.update(
        "CASE SENSITIVE SUPPLIES",
        contact_email="accounts@example.test",
    )
    assert updated.contact_email == "accounts@example.test"
    suppliers.delete("case SENSITIVE supplies")
    assert suppliers.get("Case Sensitive Supplies") is None


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
    assert client.get("/api/admin/users").status_code == 404
    assert (
        client.put(
            "/api/admin/supplier-terms/1",
            json={"company": "*", "supplier": "Supplier Ltd"},
        ).status_code
        == 403
    )


def test_local_development_identities_are_not_persisted_as_users(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    login(client, "admin", "ChangeMe-Admin1!")

    with client.app.state.auth_store._connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "users" not in tables
    assert "federated_sessions" not in tables
    assert "sessions" in tables
    assert client.get("/api/admin/users").status_code == 404


def test_auth_store_migrates_legacy_users_and_preserves_entra_sessions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-auth.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                username TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                email TEXT,
                role TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE federated_sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                display_name TEXT NOT NULL,
                email TEXT,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            INSERT INTO federated_sessions VALUES (
                'entra-token', 'entra-user', 'Entra User',
                'entra@example.test', 'purchase_ledger',
                '2026-09-21T12:00:00+00:00', '2099-09-21T12:00:00+00:00'
            );
            """
        )

    store = AuthStore(database)

    user = store.get_user_by_session("entra-token")
    assert user is not None
    assert user.username == "entra-user"
    with store._connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "users" not in tables
    assert "federated_sessions" not in tables


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
    company_updated = client.put(
        "/api/admin/companies/New%20Co%20Ltd",
        json={
            "aliases": ["New Company"],
            "vat_number": "GB123456789",
            "address": None,
        },
    )
    assert company_updated.status_code == 200, company_updated.text
    assert company_updated.json()["aliases"] == ["New Company"]
    assert company_updated.json()["vat_number"] == "GB123456789"
    assert company_updated.json()["address"] is None

    supplier = client.post(
        "/api/admin/suppliers",
        json={
            "name": "New Supplier",
            "contact_email": "ns@example.test",
            "invoice_number_pattern": "INV-######",
        },
    )
    assert supplier.status_code == 200
    supplier_updated = client.put(
        "/api/admin/suppliers/New%20Supplier",
        json={
            "aliases": ["Supplier Alias"],
            "default_company": "New Co Ltd",
            "contact_email": None,
            "invoice_number_pattern": "########@@@",
        },
    )
    assert supplier_updated.status_code == 200, supplier_updated.text
    assert supplier_updated.json()["default_company"] == "New Co Ltd"
    assert supplier_updated.json()["contact_email"] is None
    assert supplier_updated.json()["invoice_number_pattern"] == "########@@@"

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
    cleared = client.put(
        f"/api/admin/approval-matrix/{entry_id}",
        json={"approver2_name": None, "approver2_email": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["approver2_name"] is None
    assert cleared.json()["approver2_email"] is None

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
        files={"file": ("review.pdf", VALID_PDF_BYTES, "application/pdf")},
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


def test_flagged_invoice_can_be_rejected_directly(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("flagged.pdf", VALID_PDF_BYTES, "application/pdf")},
    )
    invoice_id = upload.json()["id"]
    client.app.state.invoice_store.update_fields(
        invoice_id,
        status="Needs Review",
        review_reason="Invoice details require review.",
    )

    rejected = client.post(
        f"/api/invoices/{invoice_id}/reject-flagged",
        json={"reason": "Not a valid supplier invoice."},
    )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "Rejected"
    assert rejected.json()["rejection_reason"] == "Not a valid supplier invoice."
    assert rejected.json()["review_reason"] is None


def test_purchase_ledger_can_delete_invoice_before_approval(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("delete-me.pdf", VALID_PDF_BYTES, "application/pdf")},
    )
    invoice_id = upload.json()["id"]
    stored_path = Path(upload.json()["stored_path"])

    deleted = client.delete(f"/api/invoices/{invoice_id}")

    assert deleted.status_code == 200
    assert deleted.json() == {"id": invoice_id, "deleted": True}
    assert client.get(f"/api/invoices/{invoice_id}").status_code == 404
    assert not stored_path.exists()


def test_invoice_cannot_be_deleted_after_approval_starts(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    invoice_id = _upload_and_confirm(client, supplier_invoice_number="NO-DELETE")
    client.app.state.invoice_store.update_fields(
        invoice_id,
        status="Awaiting Approval 1",
    )

    deleted = client.delete(f"/api/invoices/{invoice_id}")

    assert deleted.status_code == 422
    assert "before they enter approval or payment" in deleted.json()["detail"]
    assert client.get(f"/api/invoices/{invoice_id}").status_code == 200


def test_flagged_invoice_can_be_rejected_during_review(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("reject-review.pdf", VALID_PDF_BYTES, "application/pdf")},
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
        files={"file": ("duplicate-review.pdf", VALID_PDF_BYTES, "application/pdf")},
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
        files={"file": ("invoice.pdf", VALID_PDF_BYTES, "application/pdf")},
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
        json={"irj_number": confirm.json()["irj_number"]},
    )
    assert register.status_code == 200, register.text
    assert register.json()["status"] == "Awaiting Approval 1"
    return invoice_id


def test_full_nominal_approval_hold_resume_and_pay_flow(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    invoice_id = _upload_and_confirm(client)
    sharepoint_moves: list[tuple[object, ...]] = []
    client.app.state.lifecycle._move_pdf_in_sharepoint = (
        lambda *args: sharepoint_moves.append(args)
    )

    # Approver 1 places the invoice on hold with a required comment.
    login(client, "jordan.blake", "ChangeMe-App1!")
    hold = client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"level": 1, "decision": "on_hold", "comments": "Querying unit price with supplier."},
    )
    assert hold.status_code == 200, hold.text
    assert hold.json()["status"] == "Awaiting Approval 1"
    assert hold.json()["hold_level"] == 1
    assert "Jordan Blake (Approver 1) recorded approval query" in hold.json()["hold_reason"]
    assert "BST]" in hold.json()["hold_reason"] or "GMT]" in hold.json()["hold_reason"]
    assert "Querying unit price with supplier." in hold.json()["hold_reason"]
    assert sharepoint_moves == []

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
    assert resumed.json()["hold_level"] is None
    assert "Querying unit price with supplier." in resumed.json()["hold_reason"]
    assert "Supplier confirmed the price is correct." in resumed.json()["hold_reason"]
    assert sharepoint_moves == []

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

    # Purchase Ledger selects BACS, then records payment.
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    client.app.state.invoice_store.update_fields(
        invoice_id,
        foreign_allocation_date="2026-01-14",
        foreign_allocation_reference="STALE-FX",
        foreign_allocated_by="Previous User",
    )
    routed = client.post(
        f"/api/invoices/{invoice_id}/payment-route",
        json={"route": "bacs"},
    )
    assert routed.status_code == 200
    assert routed.json()["status"] == "Approved for Payment - BACS"
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
    # The date the payment appears on the bank statement (user-entered) is
    # distinct from the system-recorded timestamp of when the invoice was
    # marked reconciled.
    assert reconciled.json()["reconciliation_date"] == "2026-01-17"
    assert reconciled.json()["reconciled_at"] is not None
    assert reconciled.json()["reconciled_at"].startswith("2026-")


def test_concurrent_final_approval_sends_one_email(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(tmp_path)
    invoice_id = _upload_and_confirm(client)

    login(client, "jordan.blake", "ChangeMe-App1!")
    approve1 = client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"level": 1, "decision": "approved"},
    )
    assert approve1.status_code == 200
    assert approve1.json()["status"] == "Awaiting Approval 2"

    lifecycle = client.app.state.lifecycle
    claim_barrier = Barrier(2)
    moved_invoice_ids: list[int] = []
    sent_subjects: list[str] = []
    sent_subjects_lock = Lock()
    original_claim = lifecycle.invoice_store.update_fields_if_status

    def synchronized_claim(
        invoice_id: int,
        expected_status: str,
        **fields: object,
    ):
        if expected_status == "Awaiting Approval 2":
            claim_barrier.wait(timeout=5)
        return original_claim(invoice_id, expected_status, **fields)

    def capture_move(invoice, *args: object, **kwargs: object) -> None:
        moved_invoice_ids.append(invoice.id)

    def capture_email(*, recipient: str, subject: str, body: str) -> None:
        with sent_subjects_lock:
            sent_subjects.append(subject)

    monkeypatch.setattr(
        lifecycle.invoice_store,
        "update_fields_if_status",
        synchronized_claim,
    )
    monkeypatch.setattr(lifecycle, "_move_pdf_in_sharepoint", capture_move)
    monkeypatch.setattr(
        "app.invoice_lifecycle.send_email_notification",
        capture_email,
    )

    def approve() -> str:
        try:
            return lifecycle.decide_approval(
                invoice_id,
                level=2,
                decision="approved",
                comments=None,
                recorded_by="Sam Ellis",
            ).status
        except InvoiceLifecycleError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: approve(), range(2)))

    assert outcomes.count("Approved") == 1
    assert sum("is not Awaiting Approval 2" in outcome for outcome in outcomes) == 1
    assert moved_invoice_ids == [invoice_id]
    assert sent_subjects == ["Invoice (invoice.pdf) fully approved"]


def test_approval_query_cycle_does_not_repeat_stage_email(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(tmp_path)
    sent_subjects: list[str] = []
    monkeypatch.setattr(
        "app.invoice_lifecycle.send_email_notification",
        lambda *, recipient, subject, body: sent_subjects.append(subject),
    )

    invoice_id = _upload_and_confirm(client)
    assert sent_subjects == ["New invoice (invoice.pdf) waiting for approval"]

    login(client, "jordan.blake", "ChangeMe-App1!")
    hold = client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={
            "level": 1,
            "decision": "on_hold",
            "comments": "Please confirm the coding.",
        },
    )
    assert hold.status_code == 200

    login(client, "purchase.ledger", "ChangeMe-PL1!")
    resumed = client.post(
        f"/api/invoices/{invoice_id}/resume-approval",
        json={"resolution_notes": "Coding confirmed."},
    )
    assert resumed.status_code == 200
    assert sent_subjects == ["New invoice (invoice.pdf) waiting for approval"]

    lifecycle = client.app.state.lifecycle
    lifecycle._send_stage_email_once(
        invoice_id,
        "approval_1",
        recipient="jordan.blake@example.test",
        subject="Duplicate approval request",
        body="This must not be sent.",
    )
    assert sent_subjects == ["New invoice (invoice.pdf) waiting for approval"]


def test_failed_stage_email_can_be_retried_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(tmp_path)
    lifecycle = client.app.state.lifecycle
    attempts: list[str] = []

    def fail_once(*, recipient: str, subject: str, body: str) -> bool:
        attempts.append(subject)
        if len(attempts) == 1:
            raise RuntimeError("Graph unavailable")
        return True

    monkeypatch.setattr(
        "app.invoice_lifecycle.send_email_notification",
        fail_once,
    )

    with pytest.raises(RuntimeError, match="Graph unavailable"):
        lifecycle._send_stage_email_once(
            99,
            "approval_1",
            recipient="approver@example.test",
            subject="Approval required",
            body="Review invoice.",
        )
    lifecycle._send_stage_email_once(
        99,
        "approval_1",
        recipient="approver@example.test",
        subject="Approval required",
        body="Review invoice.",
    )
    lifecycle._send_stage_email_once(
        99,
        "approval_1",
        recipient="approver@example.test",
        subject="Duplicate",
        body="Must not send.",
    )

    assert attempts == ["Approval required", "Approval required"]


def test_sage_registration_stops_before_approval_when_recipient_email_is_missing(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    route = client.app.state.approval_matrix_store.find(
        "Acme Trading Ltd", "Supplier Ltd"
    )
    assert route is not None
    client.app.state.approval_matrix_store.update(
        route.id,
        approver2_email="",
    )
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("missing-email.pdf", VALID_PDF_BYTES, "application/pdf")},
    )
    invoice_id = upload.json()["id"]
    confirm = client.post(
        f"/api/invoices/{invoice_id}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": "MISSING-APPROVER-EMAIL",
            "invoice_value": 500.0,
            "currency": "GBP",
        },
    )
    registration = client.post(
        f"/api/invoices/{invoice_id}/register-sage",
        json={"irj_number": confirm.json()["irj_number"]},
    )
    assert registration.status_code == 200
    assert registration.json()["status"] == "Needs Review"
    assert "Approver 2 (Sam Ellis)" in registration.json()["review_reason"]
    assert registration.json()["approver1_email"] is None

    client.app.state.approval_matrix_store.update(
        route.id,
        approver2_email="sam.ellis@example.test",
    )
    retried = client.post(
        f"/api/invoices/{invoice_id}/register-sage",
        json={"irj_number": confirm.json()["irj_number"]},
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "Awaiting Approval 1"

    login(client, "jordan.blake", "ChangeMe-App1!")
    approved = client.post(
        f"/api/invoices/{invoice_id}/approve",
        json={"level": 1, "decision": "approved"},
    )

    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "Awaiting Approval 2"
    assert approved.json()["approver2_name"] == "Sam Ellis"
    assert approved.json()["approver2_email"] == "sam.ellis@example.test"


def test_manual_irj_company_accepts_unique_six_digit_irj_at_sage(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    client.app.state.process_configuration_store.set(
        "irj_mode:acme trading ltd", "manual"
    )
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("edit-irj.pdf", VALID_PDF_BYTES, "application/pdf")},
    )
    invoice_id = upload.json()["id"]
    confirmed = client.post(
        f"/api/invoices/{invoice_id}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": "EDIT-IRJ-1",
        },
    )
    assert confirmed.json()["irj_number"] is None

    registered = client.post(
        f"/api/invoices/{invoice_id}/register-sage",
        json={"irj_number": "000250"},
    )

    assert registered.status_code == 200
    assert registered.json()["irj_number"] == "000250"
    assert registered.json()["sage_reference"] is None

    second_upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("next-irj.pdf", VALID_PDF_BYTES, "application/pdf")},
    )
    second_confirmed = client.post(
        f"/api/invoices/{second_upload.json()['id']}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": "EDIT-IRJ-2",
        },
    )
    assert second_confirmed.json()["irj_number"] is None
    duplicate = client.post(
        f"/api/invoices/{second_upload.json()['id']}/register-sage",
        json={"irj_number": "000250"},
    )
    assert duplicate.status_code == 422
    assert "already assigned" in duplicate.json()["detail"]


def test_automatic_irj_company_rejects_edit_at_sage(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("automatic-irj.pdf", VALID_PDF_BYTES, "application/pdf")},
    )
    invoice_id = upload.json()["id"]
    confirmed = client.post(
        f"/api/invoices/{invoice_id}/confirm",
        json={
            "company": "Acme Trading Ltd",
            "supplier": "Supplier Ltd",
            "supplier_invoice_number": "AUTO-IRJ-1",
        },
    )
    assert confirmed.status_code == 200
    assigned_irj = confirmed.json()["irj_number"]
    assert assigned_irj is not None

    response = client.post(
        f"/api/invoices/{invoice_id}/register-sage",
        json={"irj_number": "000250"},
    )

    assert response.status_code == 422
    assert assigned_irj in response.json()["detail"]


def test_admin_sets_company_irj_mode_and_latest_paper_number(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    login(client, "admin", "ChangeMe-Admin1!")

    response = client.put(
        "/api/admin/irj-configurations/Acme%20Trading%20Ltd",
        json={"mode": "automatic", "current_irj": "004321"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "company": "Acme Trading Ltd",
        "mode": "automatic",
        "current_irj": "004321",
    }

    login(client, "purchase.ledger", "ChangeMe-PL1!")
    invoice_id = _upload_and_confirm(
        client, supplier_invoice_number="AUTO-IRJ-4322"
    )
    invoice = client.get(f"/api/invoices/{invoice_id}").json()
    assert invoice["irj_number"] == "004322"


def test_foreign_poa_payment_flows_through_bank_reconciliation(tmp_path: Path) -> None:
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
    routed = client.post(
        f"/api/invoices/{invoice_id}/payment-route",
        json={"route": "foreign_poa"},
    )
    assert routed.status_code == 200
    assert routed.json()["status"] == "Approved for Payment - Foreign POA"
    assert routed.json()["is_foreign_payment"] == 1
    assert routed.json()["payment_route_decided_by"] == "Purchase Ledger"
    assert routed.json()["payment_date"] is None
    assert routed.json()["payment_reference"] is None
    assert routed.json()["reconciliation_date"] is None

    paid = client.post(
        f"/api/invoices/{invoice_id}/pay",
        json={
            "payment_date": "2026-01-20",
            "payment_reference": "FX-ALLOC-9",
        },
    )
    assert paid.status_code == 200
    assert paid.json()["status"] == "Paid / Awaiting Bank Reconciliation"
    assert paid.json()["payment_method"] == "Foreign POA"

    reconciled = client.post(
        f"/api/invoices/{invoice_id}/reconcile",
        json={
            "reconciliation_date": "2026-01-22",
            "notes": "Appeared on the bank statement.",
        },
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "Reconciled / Complete"
    assert reconciled.json()["reconciliation_date"] == "2026-01-22"


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
        files={"file": ("new-supplier.pdf", VALID_PDF_BYTES, "application/pdf")},
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
        json={"irj_number": confirmed.json()["irj_number"]},
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
        json={"irj_number": confirmed.json()["irj_number"]},
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "Awaiting Approval 1"
    assert retried.json()["sage_reference"] is None


def test_po_query_resolution_requires_sage_registration_before_approval(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("po-invoice.pdf", VALID_PDF_BYTES, "application/pdf")},
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
    sharepoint_moves: list[tuple[object, ...]] = []
    client.app.state.lifecycle._move_pdf_in_sharepoint = (
        lambda *args: sharepoint_moves.append(args)
    )

    query = client.post(
        f"/api/invoices/{invoice_id}/po-match",
        json={
            "matched": False,
            "notes": "Goods receipt is missing.",
            "query_category": "missing goods receipt",
            "purchasing_contact": "Purchasing Team",
        },
    )
    assert query.json()["status"] == "Awaiting PO Matching"
    assert "Purchase Ledger recorded PO query" in query.json()["po_query_notes"]
    assert "BST]" in query.json()["po_query_notes"] or "GMT]" in query.json()["po_query_notes"]
    assert "Goods receipt is missing." in query.json()["po_query_notes"]
    assert query.json()["po_query_category"] == "missing goods receipt"
    assert sharepoint_moves == []

    matched = client.post(
        f"/api/invoices/{invoice_id}/po-match",
        json={"matched": True, "notes": "Goods receipt recorded."},
    )
    assert matched.json()["status"] == "Awaiting Sage Registration"
    assert "Goods receipt is missing." in matched.json()["po_query_notes"]
    assert "Goods receipt recorded." in matched.json()["po_query_notes"]

    registered = client.post(
        f"/api/invoices/{invoice_id}/register-sage",
        json={"irj_number": matched.json()["irj_number"]},
    )
    assert registered.status_code == 200
    assert registered.json()["status"] == "Approved"
    assert registered.json()["sage_registered_by"] == "Purchase Ledger"


def test_po_invoice_can_be_rejected_from_matching(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")
    upload = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("po-reject.pdf", VALID_PDF_BYTES, "application/pdf")},
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
        files={"file": ("invoice2.pdf", VALID_PDF_BYTES, "application/pdf")},
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
        files={"file": ("duplicate.pdf", VALID_PDF_BYTES, "application/pdf")},
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
        files={"file": ("same-invoice.pdf", VALID_PDF_BYTES, "application/pdf")},
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
        files={"file": ("same-invoice.pdf", VALID_PDF_BYTES, "application/pdf")},
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
