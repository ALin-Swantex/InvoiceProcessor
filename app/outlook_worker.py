from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Protocol

from app.environment import load_project_environment
from app.invoices import InvoiceStore
from app.outlook_graph import OutlookGraphClient, graph_client_from_environment
from app.outlook_notifications import OutlookNotificationStore


load_project_environment()


class OutlookRetriever(Protocol):
    async def list_invoice_emails(
        self, limit: int = 20, unread_only: bool = True
    ) -> list[dict[str, object]]: ...

    async def get_invoice_email(self, message_id: str) -> dict[str, object]: ...

    async def list_invoice_attachments(
        self, message_id: str
    ) -> list[dict[str, object]]: ...

    async def download_invoice_attachment(
        self, message_id: str, attachment_id: str, filename: str
    ) -> str: ...


class OutlookGraphRetriever:
    """Adapts the synchronous OutlookGraphClient to the async OutlookRetriever
    interface expected by the worker, calling Microsoft Graph directly instead
    of going through an MCP server."""

    def __init__(self, graph_client: OutlookGraphClient) -> None:
        self.graph_client = graph_client

    async def list_invoice_emails(
        self, limit: int = 20, unread_only: bool = True
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self.graph_client.list_invoice_emails,
            limit=limit,
            unread_only=unread_only,
        )

    async def get_invoice_email(self, message_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.graph_client.get_invoice_email, message_id
        )

    async def list_invoice_attachments(
        self, message_id: str
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self.graph_client.list_invoice_attachments, message_id
        )

    async def download_invoice_attachment(
        self, message_id: str, attachment_id: str, filename: str
    ) -> str:
        stored_path = await asyncio.to_thread(
            self.graph_client.download_invoice_attachment,
            message_id,
            attachment_id,
            filename,
        )
        return str(stored_path)


class OutlookInvoiceWorker:
    def __init__(
        self,
        notification_store: OutlookNotificationStore,
        invoice_store: InvoiceStore,
        retriever: OutlookRetriever,
    ) -> None:
        self.notification_store = notification_store
        self.invoice_store = invoice_store
        self.retriever = retriever

    async def enqueue_from_mailbox(self, limit: int = 20) -> int:
        messages = await self.retriever.list_invoice_emails(
            limit=limit,
            unread_only=True,
        )
        queued = 0
        for message in messages:
            message_id = self._required_string(message, "id")
            if self.notification_store.enqueue(
                subscription_id="local-mcp-polling",
                message_id=message_id,
                resource=f"local-mcp-polling/messages/{message_id}",
                change_type="created",
                payload={"source": "local-mcp-polling", "message_id": message_id},
            ):
                queued += 1
        return queued

    async def process_next(self) -> bool:
        notification = self.notification_store.claim_next()
        if notification is None:
            return False

        try:
            message = await self.retriever.get_invoice_email(
                notification.message_id
            )
            attachments = await self.retriever.list_invoice_attachments(
                notification.message_id
            )
            if not attachments:
                raise RuntimeError(
                    "The Outlook message contains no supported invoice attachments "
                    "(PDF, XLS, or XLSX)."
                )

            for attachment in attachments:
                attachment_id = self._required_string(attachment, "id")
                filename = self._required_string(attachment, "name")
                stored_path = Path(
                    await self.retriever.download_invoice_attachment(
                        notification.message_id,
                        attachment_id,
                        filename,
                    )
                )
                if not stored_path.is_file():
                    raise RuntimeError(
                        f"Microsoft Graph processing produced no PDF: {stored_path}"
                    )
                processed_attachment = dict(attachment)
                processed_attachment["name"] = stored_path.name
                processed_attachment["contentType"] = "application/pdf"
                processed_attachment["size"] = stored_path.stat().st_size
                self.invoice_store.add_from_outlook(
                    message=message,
                    attachment=processed_attachment,
                    stored_path=stored_path,
                )
        except Exception as error:
            self.notification_store.mark_failed(notification.id, str(error))
            raise

        self.notification_store.mark_completed(notification.id)
        return True

    @staticmethod
    def _required_string(data: dict[str, object], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"Outlook attachment is missing '{key}'.")
        return value


def build_worker_from_environment() -> OutlookInvoiceWorker:
    notification_store = OutlookNotificationStore(
        Path(
            os.environ.get(
                "OUTLOOK_WEBHOOK_DB_PATH",
                "runtime_data/outlook_notifications.db",
            )
        )
    )
    invoice_store = InvoiceStore(
        Path(os.environ.get("INVOICE_DB_PATH", "runtime_data/invoices.db"))
    )
    return OutlookInvoiceWorker(
        notification_store,
        invoice_store,
        OutlookGraphRetriever(graph_client_from_environment()),
    )


async def run_forever() -> None:
    worker = build_worker_from_environment()
    poll_seconds = float(os.environ.get("OUTLOOK_WORKER_POLL_SECONDS", "2"))
    local_polling_enabled = os.environ.get(
        "OUTLOOK_LOCAL_POLLING_ENABLED", "false"
    ).lower() in {"1", "true", "yes"}
    local_polling_limit = int(os.environ.get("OUTLOOK_LOCAL_POLLING_LIMIT", "20"))
    while True:
        try:
            processed = await worker.process_next()
            if not processed and local_polling_enabled:
                processed = (await worker.enqueue_from_mailbox(local_polling_limit)) > 0
        except Exception as error:
            print(f"Outlook invoice worker failed: {error}", flush=True)
            processed = False
        if not processed:
            await asyncio.sleep(poll_seconds)


if __name__ == "__main__":
    asyncio.run(run_forever())
