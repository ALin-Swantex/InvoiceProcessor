import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.invoices import InvoiceStore
from app.main import create_app
from app.outlook_notifications import OutlookNotificationStore
from app.outlook_worker import OutlookInvoiceWorker


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

    async def list_pdf_attachments(
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

    async def download_pdf_attachment(
        self, message_id: str, attachment_id: str, filename: str
    ) -> str:
        return str(self.pdf_path)


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
    pdf_path.write_bytes(b"%PDF-1.4\ninvoice\n%%EOF")
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

    client = TestClient(create_app(invoice_store=invoices))
    listed = client.get("/api/invoices")
    served_pdf = client.get(f"/api/invoices/{records[0].id}/pdf")
    assert listed.json()[0]["subject"] == "Supplier invoice 1001"
    assert served_pdf.status_code == 200
    assert served_pdf.content.startswith(b"%PDF-")


def test_worker_places_no_pdf_email_in_failed_queue(tmp_path: Path) -> None:
    pdf_path = tmp_path / "unused.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")
    notifications = queued_store(tmp_path)
    worker = OutlookInvoiceWorker(
        notifications,
        InvoiceStore(tmp_path / "invoices.db"),
        FakeOutlookRetriever(pdf_path, attachments=False),
    )

    with pytest.raises(RuntimeError, match="no PDF attachments"):
        asyncio.run(worker.process_next())

    failed = notifications.list(status="failed")
    assert len(failed) == 1
    assert failed[0].attempts == 1
    assert "no PDF attachments" in str(failed[0].last_error)


def test_worker_is_idempotent_for_duplicate_invoice_attachment(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "invoice.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")
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
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")
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
