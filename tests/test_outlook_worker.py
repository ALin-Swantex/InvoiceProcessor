import asyncio
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from app.activity_feed import ActivityFeedStore
from app.auth import AuthStore
from app.invoices import InvoiceStore
from app.main import create_app
from app.outlook_notifications import OutlookNotificationStore
from app.outlook_worker import OutlookInvoiceWorker
from app.sharepoint import SharePointClient
from app.sharepoint_intake import SharePointIncomingMonitor
from tests.pdf_helpers import VALID_PDF_BYTES


class FakeOutlookRetriever:
    def __init__(self, pdf_path: Path, *, attachments: bool = True) -> None:
        self.pdf_path = pdf_path
        self.attachments = attachments

    async def list_invoice_emails(
        self, limit: int = 20, unread_only: bool = True
    ) -> list[dict[str, object]]:
        return [{"id": "message-1", "hasAttachments": True}]

    async def get_invoice_email(self, message_id: str) -> dict[str, object]:
        return {
            "id": message_id,
            "internetMessageId": "<invoice@example.test>",
            "subject": "Supplier invoice 1001",
            "from": {
                "emailAddress": {
                    "name": "Supplier Ltd",
                    "address": "accounts@supplier.example",
                }
            },
            "receivedDateTime": "2026-04-06T10:00:00Z",
        }

    async def list_invoice_attachments(
        self, message_id: str
    ) -> list[dict[str, object]]:
        if not self.attachments:
            return []
        return [
            {
                "id": "attachment-1",
                "name": "invoice-1001.pdf",
                "size": self.pdf_path.stat().st_size,
            }
        ]

    async def download_invoice_attachment(
        self, message_id: str, attachment_id: str, filename: str
    ) -> str:
        return str(self.pdf_path)


class FakeExcelRetriever(FakeOutlookRetriever):
    async def list_invoice_attachments(
        self, message_id: str
    ) -> list[dict[str, object]]:
        return [
            {
                "id": "excel-attachment-1",
                "name": "invoice-1001.xlsx",
                "size": 1234,
            }
        ]


class FakeSharePointIncomingClient:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes]] = []

    def upload_to_incoming(
        self, filename: str, content: bytes
    ) -> dict[str, object]:
        self.uploads.append((filename, content))
        return {
            "id": "sharepoint-item-1",
            "name": filename,
            "size": len(content),
            "file": {"mimeType": "application/pdf"},
            "webUrl": "https://sharepoint.example/invoice.pdf",
        }

    def list_incoming_pdfs(self) -> list[dict[str, object]]:
        return []

    def download_item(self, item_id: str) -> bytes:
        raise AssertionError("Outlook content should be passed directly to intake")

    def get_item_web_url(self, item: dict[str, object]) -> str | None:
        return str(item["webUrl"])


def queued_store(tmp_path: Path) -> OutlookNotificationStore:
    store = OutlookNotificationStore(tmp_path / "notifications.db")
    store.enqueue(
        subscription_id="subscription-1",
        message_id="message-1",
        resource="Users/mailbox/Messages/message-1",
        change_type="created",
        payload={"resourceData": {"id": "message-1"}},
    )
    return store


def test_worker_persists_outlook_pdf_and_completes_notification(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "invoice-1001.pdf"
    pdf_path.write_bytes(VALID_PDF_BYTES)
    notifications = queued_store(tmp_path)
    invoices = InvoiceStore(tmp_path / "invoices.db")
    worker = OutlookInvoiceWorker(
        notifications,
        invoices,
        FakeOutlookRetriever(pdf_path),
    )

    assert asyncio.run(worker.process_next()) is True

    records = invoices.list()
    assert len(records) == 1
    assert records[0].sender_address == "accounts@supplier.example"
    assert records[0].status == "Awaiting AI Extraction"
    completed = notifications.list(status="completed")
    assert len(completed) == 1
    assert completed[0].attempts == 1

    client = TestClient(
        create_app(
            invoice_store=invoices,
            auth_store=AuthStore(tmp_path / "auth.db"),
            activity_feed=ActivityFeedStore(tmp_path / "activity_feed.db"),
            notification_store=OutlookNotificationStore(
                tmp_path / "outlook_notifications.db"
            ),
        )
    )
    login = client.post(
        "/api/auth/login",
        json={"username": "purchase.ledger", "password": "ChangeMe-PL1!"},
    )
    assert login.status_code == 200
    listed = client.get("/api/invoices")
    served_pdf = client.get(f"/api/invoices/{records[0].id}/pdf")
    assert listed.json()[0]["subject"] == "Supplier invoice 1001"
    assert served_pdf.status_code == 200
    assert served_pdf.content.startswith(b"%PDF-")


def test_worker_runs_configured_extraction_after_outlook_intake(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "invoice-1001.pdf"
    pdf_path.write_bytes(VALID_PDF_BYTES)
    invoices = InvoiceStore(tmp_path / "invoices.db")
    extracted_ids: list[int] = []
    worker = OutlookInvoiceWorker(
        queued_store(tmp_path),
        invoices,
        FakeOutlookRetriever(pdf_path),
        extraction_runner=extracted_ids.append,
    )

    assert asyncio.run(worker.process_next()) is True

    assert extracted_ids == [invoices.list()[0].id]


def test_worker_uploads_outlook_pdf_to_sharepoint_before_registration(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "invoice-1001.pdf"
    content = VALID_PDF_BYTES
    pdf_path.write_bytes(content)
    invoices = InvoiceStore(tmp_path / "invoices.db")
    fake_sharepoint = FakeSharePointIncomingClient()
    extracted_ids: list[int] = []
    monitor = SharePointIncomingMonitor(
        cast(SharePointClient, fake_sharepoint),
        invoices,
        cache_directory=tmp_path / "cache",
        extraction_runner=extracted_ids.append,
    )
    worker = OutlookInvoiceWorker(
        queued_store(tmp_path),
        invoices,
        FakeOutlookRetriever(pdf_path),
        incoming_monitor=monitor,
    )

    assert asyncio.run(worker.process_next()) is True

    record = invoices.list()[0]
    assert fake_sharepoint.uploads == [("invoice-1001.pdf", content)]
    assert record.sharepoint_item_id == "sharepoint-item-1"
    assert record.sender_address == "accounts@supplier.example"
    assert extracted_ids == [record.id]


def test_worker_persists_converted_excel_as_a_pdf(tmp_path: Path) -> None:
    converted_pdf = tmp_path / "invoice-1001.pdf"
    converted_pdf.write_bytes(VALID_PDF_BYTES)
    notifications = queued_store(tmp_path)
    invoices = InvoiceStore(tmp_path / "invoices.db")
    worker = OutlookInvoiceWorker(
        notifications,
        invoices,
        FakeExcelRetriever(converted_pdf),
    )

    assert asyncio.run(worker.process_next()) is True

    record = invoices.list()[0]
    assert record.attachment_id == "excel-attachment-1"
    assert record.original_filename == "invoice-1001.pdf"
    assert record.stored_path == str(converted_pdf)
    assert record.size_bytes == converted_pdf.stat().st_size


def test_worker_places_no_pdf_email_in_failed_queue(tmp_path: Path) -> None:
    pdf_path = tmp_path / "unused.pdf"
    pdf_path.write_bytes(VALID_PDF_BYTES)
    notifications = queued_store(tmp_path)
    worker = OutlookInvoiceWorker(
        notifications,
        InvoiceStore(tmp_path / "invoices.db"),
        FakeOutlookRetriever(pdf_path, attachments=False),
    )

    with pytest.raises(RuntimeError, match="no supported invoice attachments"):
        asyncio.run(worker.process_next())

    failed = notifications.list(status="failed")
    assert len(failed) == 1
    assert failed[0].attempts == 1
    assert "no supported invoice attachments" in str(failed[0].last_error)


def test_worker_is_idempotent_for_duplicate_invoice_attachment(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "invoice.pdf"
    pdf_path.write_bytes(VALID_PDF_BYTES)
    notifications = queued_store(tmp_path)
    invoices = InvoiceStore(tmp_path / "invoices.db")
    worker = OutlookInvoiceWorker(
        notifications,
        invoices,
        FakeOutlookRetriever(pdf_path),
    )

    asyncio.run(worker.process_next())
    notifications.enqueue(
        subscription_id="subscription-2",
        message_id="message-1",
        resource="Users/mailbox/Messages/message-1",
        change_type="created",
        payload={"resourceData": {"id": "message-1"}},
    )
    asyncio.run(worker.process_next())

    assert len(invoices.list()) == 1


def test_local_polling_enqueues_unread_outlook_messages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "invoice.pdf"
    pdf_path.write_bytes(VALID_PDF_BYTES)
    notifications = OutlookNotificationStore(tmp_path / "notifications.db")
    worker = OutlookInvoiceWorker(
        notifications,
        InvoiceStore(tmp_path / "invoices.db"),
        FakeOutlookRetriever(pdf_path),
    )

    assert asyncio.run(worker.enqueue_from_mailbox()) == 1
    assert asyncio.run(worker.enqueue_from_mailbox()) == 0

    pending = notifications.list()
    assert len(pending) == 1
    assert pending[0].message_id == "message-1"
    assert pending[0].subscription_id == "local-mcp-polling"
