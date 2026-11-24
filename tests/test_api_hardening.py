"""Hardening of the API routers found in the codebase review."""

import io
import logging
import zipfile
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from app.activity_feed import ActivityFeedStore
from app.auth import AuthStore
from app.invoices import InvoiceStore
from app.main import create_app
from app.outlook_notifications import OutlookNotificationStore
from app.sharepoint import SharePointClient
from tests.pdf_helpers import VALID_PDF_BYTES
from tests.test_api import client_for

CLIENT_STATE = "test-client-state"


def login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text


def add_invoice(
    client: TestClient,
    tmp_path: Path,
    name: str,
    *,
    company: str = "Acme Trading Ltd",
    irj_number: str | None = None,
    approver1_email: str = "someone.else@example.test",
) -> int:
    store = client.app.state.invoice_store
    pdf = tmp_path / f"{name}.pdf"
    pdf.write_bytes(VALID_PDF_BYTES)
    invoice = store.add_from_outlook(
        message={"id": f"message-{name}"},
        attachment={"id": f"attachment-{name}", "name": f"{name}.pdf"},
        stored_path=pdf,
    )
    return store.update_fields(
        invoice.id,
        company=company,
        supplier="Supplier Ltd",
        irj_number=irj_number,
        status="Awaiting Approval 1",
        approver1_name="Approver",
        approver1_email=approver1_email,
    ).id


# 1. Local login alongside Entra


def test_warns_when_local_login_is_enabled_with_entra(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="invoice_processor"):
        create_app(
            invoice_store=InvoiceStore(tmp_path / "invoices.db"),
            auth_store=AuthStore(tmp_path / "auth.db"),
            activity_feed=ActivityFeedStore(tmp_path / "activity.db"),
            notification_store=OutlookNotificationStore(tmp_path / "n.db"),
            entra_auth_client=object(),  # type: ignore[arg-type]
        )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("AUTH_LOCAL_LOGIN_ENABLED" in r.getMessage() for r in warnings)


def test_no_local_login_warning_without_entra(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="invoice_processor"):
        client_for(tmp_path)
    assert not any(
        "AUTH_LOCAL_LOGIN_ENABLED" in r.getMessage() for r in caplog.records
    )


# 2. Activity cursor for approvers


def test_approver_activity_skips_past_a_full_page_of_other_events(
    tmp_path: Path,
) -> None:
    client = client_for(tmp_path)
    other_id = add_invoice(client, tmp_path, "other")
    own_id = add_invoice(
        client, tmp_path, "own", approver1_email="jordan.blake@example.test"
    )
    feed = client.app.state.activity_feed
    for index in range(55):
        feed.add_event(
            event_type="approval_pending",
            target_role="approver1",
            message=f"Other {index}",
            invoice_id=other_id,
        )
    feed.add_event(
        event_type="approval_pending",
        target_role="approver1",
        message="Own event",
        invoice_id=own_id,
    )
    login(client, "jordan.blake", "ChangeMe-App1!")

    events = client.get("/api/activity", params={"since_id": 0}).json()

    assert [event["message"] for event in events] == ["Own event"]


# 3. IRJ search across companies


def test_irj_search_finds_approvers_invoice_when_irj_is_shared(
    tmp_path: Path,
) -> None:
    client = client_for(tmp_path)
    add_invoice(client, tmp_path, "first", company="Alpha Ltd", irj_number="100001")
    own_id = add_invoice(
        client,
        tmp_path,
        "second",
        company="Beta Ltd",
        irj_number="100001",
        approver1_email="jordan.blake@example.test",
    )
    login(client, "jordan.blake", "ChangeMe-App1!")

    response = client.get("/api/invoice-search", params={"irj_number": "100001"})

    assert response.status_code == 200, response.text
    assert response.json()["id"] == own_id


# 4. Manual upload


class RecordingSharePointClient:
    def __init__(self) -> None:
        self.uploads: list[str] = []

    def upload_to_incoming(self, filename: str, content: bytes) -> dict[str, object]:
        self.uploads.append(filename)
        raise AssertionError("must not upload")


def test_manual_upload_requires_pdf_filename_before_sharepoint_upload(
    tmp_path: Path,
) -> None:
    fake = RecordingSharePointClient()
    client = TestClient(
        create_app(
            invoice_store=InvoiceStore(tmp_path / "invoices.db"),
            auth_store=AuthStore(tmp_path / "auth.db"),
            activity_feed=ActivityFeedStore(tmp_path / "activity.db"),
            notification_store=OutlookNotificationStore(tmp_path / "n.db"),
            sharepoint_client=cast(SharePointClient, fake),
        )
    )
    login(client, "purchase.ledger", "ChangeMe-PL1!")

    response = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("invoice.txt", VALID_PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 400
    assert fake.uploads == []


def test_manual_upload_offline_ignores_directories_in_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.main.ai_extraction_configured", lambda: False)
    monkeypatch.setenv("MANUAL_UPLOAD_DIR", str(tmp_path / "uploads"))
    client = client_for(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")

    response = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("nested/dir/invoice.pdf", VALID_PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 200, response.text
    assert response.json()["original_filename"] == "invoice.pdf"
    stored = Path(response.json()["stored_path"])
    assert stored.parent == tmp_path / "uploads"


def test_manual_upload_rejects_oversized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.routers.invoices.MAX_MANUAL_UPLOAD_BYTES", 16)
    monkeypatch.setenv("MANUAL_UPLOAD_DIR", str(tmp_path / "uploads"))
    client = client_for(tmp_path)
    login(client, "purchase.ledger", "ChangeMe-PL1!")

    response = client.post(
        "/api/invoices/manual-upload",
        files={"file": ("invoice.pdf", VALID_PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 413


# 5. Approver decisions on invoices assigned to someone else


def test_approving_another_approvers_invoice_looks_like_missing(
    tmp_path: Path,
) -> None:
    client = client_for(tmp_path)
    other_id = add_invoice(client, tmp_path, "other")
    login(client, "jordan.blake", "ChangeMe-App1!")

    body = {"level": 1, "decision": "approved"}
    other = client.post(f"/api/invoices/{other_id}/approve", json=body)
    missing = client.post("/api/invoices/999999/approve", json=body)

    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json()


# 6. Bulk import of a file that is not a workbook


@pytest.mark.parametrize(
    "contents",
    [b"not a zip file", "zip-without-workbook"],
)
def test_bulk_import_rejects_files_that_are_not_workbooks(
    tmp_path: Path, contents: object
) -> None:
    if contents == "zip-without-workbook":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("hello.txt", "hello")
        contents = buffer.getvalue()
    client = client_for(tmp_path)
    login(client, "admin", "ChangeMe-Admin1!")

    response = client.post(
        "/api/admin/import/supplier-master-data",
        data={"company": "Acme Trading Ltd"},
        files={"file": ("suppliers.xlsx", contents, "application/octet-stream")},
    )

    assert response.status_code == 422, response.text


# 7. Outlook webhooks


def webhook_client(tmp_path: Path) -> tuple[TestClient, OutlookNotificationStore]:
    store = OutlookNotificationStore(tmp_path / "notifications.db")
    client = TestClient(
        create_app(
            notification_store=store,
            webhook_client_state=CLIENT_STATE,
            auth_store=AuthStore(tmp_path / "auth.db"),
            invoice_store=InvoiceStore(tmp_path / "invoices.db"),
            activity_feed=ActivityFeedStore(tmp_path / "activity_feed.db"),
        )
    )
    return client, store


def notification(message_id: str, client_state: str = CLIENT_STATE) -> dict[str, object]:
    return {
        "subscriptionId": "subscription-1",
        "changeType": "created",
        "clientState": client_state,
        "resource": f"Users/mailbox/Messages/{message_id}",
        "resourceData": {"id": message_id},
    }


@pytest.mark.parametrize(
    "path", ["/api/outlook/notifications", "/api/outlook/lifecycle"]
)
def test_webhook_rejects_non_json_body(tmp_path: Path, path: str) -> None:
    client, _ = webhook_client(tmp_path)

    response = client.post(
        path, content=b"not json", headers={"content-type": "application/json"}
    )

    assert response.status_code == 400


def test_bad_client_state_late_in_batch_enqueues_nothing(tmp_path: Path) -> None:
    client, store = webhook_client(tmp_path)
    payload = {
        "value": [notification("message-1"), notification("message-2", "wrong")]
    }

    response = client.post("/api/outlook/notifications", json=payload)

    assert response.status_code == 401
    assert store.list() == []


def test_missing_fields_late_in_batch_enqueues_nothing(tmp_path: Path) -> None:
    client, store = webhook_client(tmp_path)
    broken = notification("message-2")
    del broken["subscriptionId"]

    response = client.post(
        "/api/outlook/notifications",
        json={"value": [notification("message-1"), broken]},
    )

    assert response.status_code == 400
    assert store.list() == []


# 8. Approval matrix


def test_approval_matrix_is_limited_to_purchase_ledger_and_admin(
    tmp_path: Path,
) -> None:
    client = client_for(tmp_path)
    login(client, "jordan.blake", "ChangeMe-App1!")
    assert client.get("/api/approval-matrix").status_code == 403

    login(client, "purchase.ledger", "ChangeMe-PL1!")
    assert client.get("/api/approval-matrix").status_code == 200
