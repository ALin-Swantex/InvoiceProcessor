from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from app.ai_extraction import ai_extraction_configured
from app.environment import load_project_environment, project_path_from_environment
from app.invoice_lifecycle import InvoiceLifecycle
from app.invoices import InvoiceStore
from app.outlook_graph import OutlookGraphClient, graph_client_from_environment
from app.outlook_notifications import OutlookNotificationStore
from app.pdf_validation import validate_pdf
from app.sharepoint import sharepoint_client_from_environment
from app.sharepoint_intake import SharePointIncomingMonitor


load_project_environment()


class OutlookRetriever(Protocol):
    async def list_invoice_emails(
        self,
        limit: int = 20,
        unread_only: bool = True,
        received_since: datetime | None = None,
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
        self,
        limit: int = 20,
        unread_only: bool = True,
        received_since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self.graph_client.list_invoice_emails,
            limit=limit,
            unread_only=unread_only,
            received_since=received_since,
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
        extraction_runner: Callable[[int], object] | None = None,
        incoming_monitor: SharePointIncomingMonitor | None = None,
    ) -> None:
        self.notification_store = notification_store
        self.invoice_store = invoice_store
        self.retriever = retriever
        self.extraction_runner = extraction_runner
        self.incoming_monitor = incoming_monitor

    async def enqueue_from_mailbox(
        self,
        limit: int = 20,
        *,
        received_since: datetime | None = None,
    ) -> int:
        if received_since is None:
            received_since = self._local_polling_start()
        messages = await self.retriever.list_invoice_emails(
            limit=limit,
            unread_only=True,
            received_since=received_since,
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

    @staticmethod
    def _local_polling_start() -> datetime:
        try:
            lookback_days = int(
                os.environ.get("OUTLOOK_LOCAL_POLLING_LOOKBACK_DAYS", "0")
            )
        except ValueError as error:
            raise RuntimeError(
                "OUTLOOK_LOCAL_POLLING_LOOKBACK_DAYS must be a whole number."
            ) from error
        if lookback_days < 0:
            raise RuntimeError(
                "OUTLOOK_LOCAL_POLLING_LOOKBACK_DAYS cannot be negative."
            )
        local_now = datetime.now(ZoneInfo("Europe/London"))
        local_start = (local_now - timedelta(days=lookback_days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return local_start.astimezone(timezone.utc)

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
                existing = self.invoice_store.get_by_source_attachment(
                    notification.message_id, attachment_id
                )
                if existing is not None and existing.sharepoint_item_id:
                    continue
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
                processed_attachment["name"] = (
                    Path(filename).with_suffix(".pdf").name
                )
                processed_attachment["contentType"] = "application/pdf"
                processed_attachment["size"] = stored_path.stat().st_size
                if self.incoming_monitor is not None:
                    content = stored_path.read_bytes()
                    validate_pdf(content)
                    item = await asyncio.to_thread(
                        self.incoming_monitor.client.upload_to_incoming,
                        self._outlook_upload_filename(
                            notification.message_id,
                            attachment_id,
                            stored_path.name,
                        ),
                        content,
                        conflict_behavior="replace",
                    )
                    record = await asyncio.to_thread(
                        self.incoming_monitor.ingest_item,
                        item,
                        source_message=message,
                        source_attachment=processed_attachment,
                        content=content,
                        event_type="outlook_intake",
                    )
                    if (
                        record is not None
                        and stored_path != Path(record.stored_path)
                    ):
                        stored_path.unlink(missing_ok=True)
                else:
                    record = self.invoice_store.add_from_outlook(
                        message=message,
                        attachment=processed_attachment,
                        stored_path=stored_path,
                    )
                    if (
                        self.extraction_runner is not None
                        and record.status == "Awaiting AI Extraction"
                    ):
                        await asyncio.to_thread(self.extraction_runner, record.id)
        except Exception as error:
            self.notification_store.mark_failed(notification.id, str(error))
            raise

        self.notification_store.mark_completed(notification.id)
        return True

    async def scan_sharepoint_incoming(self) -> int:
        if self.incoming_monitor is None:
            return 0
        return await asyncio.to_thread(self.incoming_monitor.scan_once)

    @staticmethod
    def _required_string(data: dict[str, object], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"Outlook attachment is missing '{key}'.")
        return value

    @staticmethod
    def _outlook_upload_filename(
        message_id: str, attachment_id: str, filename: str
    ) -> str:
        source_key = f"{message_id}\0{attachment_id}".encode("utf-8")
        digest = hashlib.sha256(source_key).hexdigest()[:20]
        return f"outlook-{digest}-{Path(filename).name}"


def build_worker_from_environment() -> OutlookInvoiceWorker:
    from app.postgres_config import postgres_config_stores
    from app.postgres_invoices import create_postgres_invoice_store
    from app.postgres_notifications import PostgresOutlookNotificationStore
    from app.postgres_services import PostgresActivityFeedStore, PostgresIrjNumberGenerator

    notification_store = PostgresOutlookNotificationStore()
    invoice_store = create_postgres_invoice_store()
    irj_generator = PostgresIrjNumberGenerator()
    activity_feed = PostgresActivityFeedStore()
    (
        companies_store,
        suppliers_store,
        approval_matrix_store,
        _supplier_terms_store,
        process_configuration_store,
    ) = postgres_config_stores()
    sharepoint_client = sharepoint_client_from_environment()
    lifecycle = InvoiceLifecycle(
        invoice_store,
        irj_generator,
        activity_feed,
        sharepoint_client,
        companies_store=companies_store,
        approval_matrix_store=approval_matrix_store,
        suppliers_store=suppliers_store,
        configuration_getter=process_configuration_store.get,
    )
    extraction_runner: Callable[[int], object] = (
        lifecycle.run_extraction
        if ai_extraction_configured()
        else lifecycle.run_document_classification
    )
    incoming_monitor = SharePointIncomingMonitor(
        sharepoint_client,
        invoice_store,
        cache_directory=project_path_from_environment(
            "SHAREPOINT_INVOICE_CACHE_DIR", "runtime_data/sharepoint_invoice_cache"
        ),
        extraction_runner=extraction_runner,
        activity_feed=activity_feed,
    )
    return OutlookInvoiceWorker(
        notification_store,
        invoice_store,
        OutlookGraphRetriever(graph_client_from_environment()),
        extraction_runner,
        incoming_monitor,
    )


async def run_forever() -> None:
    from app.postgres_monitoring import PostgresWorkerMonitor

    worker = build_worker_from_environment()
    monitor = PostgresWorkerMonitor()
    worker_name = "outlook-invoice-worker"

    def report_heartbeat(**values: object) -> None:
        try:
            monitor.heartbeat(worker_name, **values)  # type: ignore[arg-type]
        except Exception as error:
            # Monitoring must never prevent the worker from processing invoices.
            print(f"Outlook worker heartbeat failed: {error}", flush=True)

    report_heartbeat(status="starting")
    last_heartbeat = 0.0
    poll_seconds = float(
        os.environ.get(
            "SHAREPOINT_INCOMING_POLL_SECONDS",
            os.environ.get("OUTLOOK_WORKER_POLL_SECONDS", "2"),
        )
    )
    local_polling_enabled = os.environ.get(
        "OUTLOOK_LOCAL_POLLING_ENABLED", "false"
    ).lower() in {"1", "true", "yes"}
    local_polling_limit = int(os.environ.get("OUTLOOK_LOCAL_POLLING_LIMIT", "20"))
    while True:
        try:
            processed = await worker.process_next()
            if not processed and local_polling_enabled:
                processed = (await worker.enqueue_from_mailbox(local_polling_limit)) > 0
            ingested = await worker.scan_sharepoint_incoming()
            processed = processed or ingested > 0
            now = time.monotonic()
            if processed or now - last_heartbeat >= 30:
                report_heartbeat(
                    status="processed" if processed else "idle",
                    success=True,
                    details={"processed_work": processed},
                )
                last_heartbeat = now
        except Exception as error:
            print(f"Outlook invoice worker failed: {error}", flush=True)
            report_heartbeat(
                status="error",
                error=str(error),
            )
            processed = False
        if not processed:
            await asyncio.sleep(poll_seconds)


if __name__ == "__main__":
    asyncio.run(run_forever())
