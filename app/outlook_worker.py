from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from app.ai_extraction import ai_extraction_configured
from app.environment import load_project_environment, project_path_from_environment
from app.invoice_lifecycle import InvoiceLifecycle
from app.invoices import InvoiceStore
from app.outlook_graph import (
    SAFE_FILENAME_PATTERN,
    OutlookGraphClient,
    graph_client_from_environment,
)
from app.outlook_notifications import OutlookNotificationStore
from app.pdf_validation import validate_pdf
from app.sharepoint import sharepoint_client_from_environment
from app.sharepoint_intake import SharePointIncomingMonitor
from app.statuses import Status


logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 3
DEFAULT_STALE_PROCESSING_SECONDS = 900.0
CANCELLED_CLAIM_NOTE = (
    "The worker stopped before finishing this item; it was returned to the queue."
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number.") from error


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a whole number.") from error


@dataclass(frozen=True)
class _PreparedAttachment:
    attachment_id: str
    stored_path: Path
    attachment: dict[str, object]
    content: bytes | None = None
    sharepoint_item: dict[str, object] | None = None


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
        scheduled_notification_runner: Callable[[], int] | None = None,
        *,
        concurrency: int | None = None,
        claim_keepalive_seconds: float | None = None,
    ) -> None:
        self.notification_store = notification_store
        self.invoice_store = invoice_store
        self.retriever = retriever
        self.extraction_runner = extraction_runner
        self.incoming_monitor = incoming_monitor
        self.scheduled_notification_runner = scheduled_notification_runner
        if concurrency is None:
            concurrency = _env_int("OUTLOOK_WORKER_CONCURRENCY", DEFAULT_CONCURRENCY)
        self.concurrency = max(1, concurrency)
        self.claim_keepalive_seconds = claim_keepalive_seconds

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

        keepalive = self._start_claim_keepalive(notification.id)
        try:
            await self._process_notification(notification.message_id)
        except asyncio.CancelledError:
            # Shutdown interrupted this item: hand it back instead of leaving
            # it stuck in 'processing'. Re-processing is safe because every
            # attachment step is idempotent (source-attachment lookup and a
            # deterministic SharePoint upload name with 'replace').
            self._release_claim(notification.id)
            raise
        except Exception as error:
            self.notification_store.mark_failed(notification.id, str(error))
            raise
        finally:
            if keepalive is not None:
                keepalive.cancel()

        self.notification_store.mark_completed(notification.id)
        return True

    async def _process_notification(self, message_id: str) -> None:
        message, attachments = await asyncio.gather(
            self.retriever.get_invoice_email(message_id),
            self.retriever.list_invoice_attachments(message_id),
        )
        if not attachments:
            # The webhook queues every inbox message, so most of these are
            # ordinary mail: skip them rather than fail the queue item.
            logger.info(
                "Outlook message %s has no PDF, XLS or XLSX attachments; skipped.",
                message_id,
            )
            return

        # Downloads, conversions and SharePoint uploads run concurrently (bounded
        # by the semaphore). Registration - duplicate detection, the invoice row
        # and extraction - stays strictly sequential and in attachment order, so
        # it behaves exactly as the old one-by-one loop did.
        semaphore = asyncio.Semaphore(self.concurrency)
        tasks = [
            asyncio.create_task(
                self._prepare_attachment(message_id, attachment, semaphore)
            )
            for attachment in attachments
        ]
        errors: list[BaseException] = []
        try:
            for task in tasks:
                try:
                    prepared = await task
                    if prepared is not None:
                        await self._register_attachment(message, prepared)
                except Exception as error:
                    errors.append(error)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        if errors:
            for extra in errors[1:]:
                logger.warning(
                    "Another attachment of Outlook message %s also failed: %s",
                    message_id,
                    extra,
                )
            raise errors[0]

    async def _prepare_attachment(
        self,
        message_id: str,
        attachment: dict[str, object],
        semaphore: asyncio.Semaphore,
    ) -> _PreparedAttachment | None:
        attachment_id = self._required_string(attachment, "id")
        filename = self._required_string(attachment, "name")
        existing = self.invoice_store.get_by_source_attachment(
            message_id, attachment_id
        )
        if existing is not None and existing.sharepoint_item_id:
            return None
        async with semaphore:
            stored_path = Path(
                await self.retriever.download_invoice_attachment(
                    message_id,
                    attachment_id,
                    filename,
                )
            )
            if not stored_path.is_file():
                raise RuntimeError(
                    f"Microsoft Graph processing produced no PDF: {stored_path}"
                )
            processed_attachment = dict(attachment)
            processed_attachment["name"] = Path(filename).with_suffix(".pdf").name
            processed_attachment["contentType"] = "application/pdf"
            processed_attachment["size"] = stored_path.stat().st_size
            if self.incoming_monitor is None:
                return _PreparedAttachment(
                    attachment_id, stored_path, processed_attachment
                )
            content = stored_path.read_bytes()
            validate_pdf(content)
            item = await asyncio.to_thread(
                self.incoming_monitor.client.upload_to_incoming,
                # From the attachment name, not the local file: that one gets a
                # "_2" suffix when an earlier download is still on disk, which
                # would upload a second copy instead of replacing the first.
                self._outlook_upload_filename(
                    message_id,
                    attachment_id,
                    processed_attachment["name"],
                ),
                content,
                conflict_behavior="replace",
            )
            return _PreparedAttachment(
                attachment_id, stored_path, processed_attachment, content, item
            )

    async def _register_attachment(
        self, message: dict[str, Any], prepared: _PreparedAttachment
    ) -> None:
        stored_path = prepared.stored_path
        if self.incoming_monitor is not None:
            assert prepared.sharepoint_item is not None
            record = None
            try:
                record = await asyncio.to_thread(
                    self.incoming_monitor.ingest_item,
                    prepared.sharepoint_item,
                    source_message=message,
                    source_attachment=prepared.attachment,
                    content=prepared.content,
                    event_type="outlook_intake",
                )
            finally:
                # Intake keeps its own copy; a retry downloads again.
                if record is None or stored_path != Path(record.stored_path):
                    stored_path.unlink(missing_ok=True)
            return
        record = self.invoice_store.add_from_outlook(
            message=message,
            attachment=prepared.attachment,
            stored_path=stored_path,
        )
        if (
            self.extraction_runner is not None
            and record.status == Status.AWAITING_EXTRACTION
        ):
            await asyncio.to_thread(self.extraction_runner, record.id)

    def _start_claim_keepalive(self, notification_id: int) -> asyncio.Task[None] | None:
        touch = getattr(self.notification_store, "touch_claim", None)
        interval = self.claim_keepalive_seconds
        if touch is None or not interval or interval <= 0:
            return None

        async def keep_alive() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    await asyncio.to_thread(touch, notification_id)
                except Exception:
                    logger.exception(
                        "Could not refresh the claim on Outlook queue item %s.",
                        notification_id,
                    )

        return asyncio.create_task(keep_alive())

    def _release_claim(self, notification_id: int) -> None:
        try:
            self.notification_store.mark_failed(notification_id, CANCELLED_CLAIM_NOTE)
            self.notification_store.retry_failed(notification_id)
            logger.warning(
                "Returned Outlook queue item %s to pending after cancellation.",
                notification_id,
            )
        except Exception:
            logger.exception(
                "Could not release Outlook queue item %s; it will be reclaimed "
                "once it is older than OUTLOOK_PROCESSING_STALE_SECONDS.",
                notification_id,
            )

    def reclaim_stale_notifications(self, older_than_seconds: float) -> int:
        reclaim = getattr(self.notification_store, "reclaim_stale", None)
        if reclaim is None:
            return 0
        return int(reclaim(older_than_seconds))

    async def scan_sharepoint_incoming(self) -> int:
        if self.incoming_monitor is None:
            return 0
        return await asyncio.to_thread(self.incoming_monitor.scan_once)

    async def send_scheduled_notifications(self) -> int:
        if self.scheduled_notification_runner is None:
            return 0
        return await asyncio.to_thread(self.scheduled_notification_runner)

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
        safe_name = SAFE_FILENAME_PATTERN.sub("_", Path(filename).name.strip())[:180]
        return f"outlook-{digest}-{safe_name}"


def build_worker_from_environment(
    *, claim_keepalive_seconds: float | None = None
) -> OutlookInvoiceWorker:
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
        supplier_terms_store=_supplier_terms_store,
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
        lifecycle.process_scheduled_notifications,
        claim_keepalive_seconds=claim_keepalive_seconds,
    )


@dataclass(frozen=True)
class WorkerLoopSettings:
    poll_seconds: float = 2.0
    sharepoint_scan_seconds: float = 2.0
    reminder_scan_seconds: float = 300.0
    heartbeat_seconds: float = 30.0
    local_polling_enabled: bool = False
    local_polling_limit: int = 20
    stale_processing_seconds: float = DEFAULT_STALE_PROCESSING_SECONDS
    stale_check_seconds: float = 60.0

    @classmethod
    def from_environment(cls) -> WorkerLoopSettings:
        poll_seconds = _env_float("OUTLOOK_WORKER_POLL_SECONDS", 2.0)
        return cls(
            poll_seconds=poll_seconds,
            # Unset -> same cadence as queue polling (previous behaviour).
            sharepoint_scan_seconds=_env_float(
                "SHAREPOINT_INCOMING_POLL_SECONDS", poll_seconds
            ),
            reminder_scan_seconds=_env_float("APPROVAL_REMINDER_SCAN_SECONDS", 300.0),
            local_polling_enabled=os.environ.get(
                "OUTLOOK_LOCAL_POLLING_ENABLED", "false"
            ).lower()
            in {"1", "true", "yes"},
            local_polling_limit=_env_int("OUTLOOK_LOCAL_POLLING_LIMIT", 20),
            stale_processing_seconds=_env_float(
                "OUTLOOK_PROCESSING_STALE_SECONDS", DEFAULT_STALE_PROCESSING_SECONDS
            ),
        )


def _due(last: float | None, interval: float, now: float) -> bool:
    return last is None or now - last >= interval


async def run_worker_loop(
    worker: OutlookInvoiceWorker,
    settings: WorkerLoopSettings,
    *,
    stop_event: asyncio.Event,
    report_heartbeat: Callable[..., None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run the worker until ``stop_event`` is set.

    The current queue item always finishes (or fails) normally; no new item is
    claimed once a stop has been requested.
    """

    def heartbeat(**values: object) -> None:
        if report_heartbeat is not None:
            report_heartbeat(**values)

    last_heartbeat: float | None = None
    last_scan: float | None = None
    last_reminder_scan: float | None = None
    last_stale_check: float | None = None
    while not stop_event.is_set():
        processed = False
        try:
            if settings.stale_processing_seconds > 0 and _due(
                last_stale_check, settings.stale_check_seconds, clock()
            ):
                last_stale_check = clock()
                reclaimed = await asyncio.to_thread(
                    worker.reclaim_stale_notifications,
                    settings.stale_processing_seconds,
                )
                if reclaimed:
                    logger.warning(
                        "Reclaimed %d Outlook queue item(s) stuck in processing.",
                        reclaimed,
                    )
            processed = await worker.process_next()
            if not processed and settings.local_polling_enabled and not stop_event.is_set():
                processed = (
                    await worker.enqueue_from_mailbox(settings.local_polling_limit)
                ) > 0
            if not stop_event.is_set() and _due(
                last_scan, settings.sharepoint_scan_seconds, clock()
            ):
                last_scan = clock()
                ingested = await worker.scan_sharepoint_incoming()
                processed = processed or ingested > 0
            now = clock()
            if not stop_event.is_set() and _due(
                last_reminder_scan, settings.reminder_scan_seconds, now
            ):
                reminders = await worker.send_scheduled_notifications()
                processed = processed or reminders > 0
                last_reminder_scan = now
            now = clock()
            if processed or _due(last_heartbeat, settings.heartbeat_seconds, now):
                heartbeat(
                    status="processed" if processed else "idle",
                    success=True,
                    details={"processed_work": processed},
                )
                last_heartbeat = now
        except Exception as error:
            logger.exception("Outlook invoice worker failed: %s", error)
            heartbeat(status="error", error=str(error))
            processed = False
        if not processed and not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.poll_seconds)
            except asyncio.TimeoutError:
                pass
    logger.info("Outlook invoice worker stopped.")
    heartbeat(status="stopped")


def install_signal_handlers(
    stop_event: asyncio.Event,
    task: asyncio.Task[Any] | None = None,
) -> list[signal.Signals]:
    """First SIGTERM/SIGINT: finish the current item, then stop.
    Second signal: cancel the running item (its claim is released)."""
    loop = asyncio.get_running_loop()

    def handle(signal_name: str) -> None:
        if not stop_event.is_set():
            logger.info(
                "Received %s; finishing the current item, then stopping.", signal_name
            )
            stop_event.set()
        elif task is not None and not task.done():
            logger.warning("Received %s again; cancelling the current item.", signal_name)
            task.cancel()

    installed: list[signal.Signals] = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, handle, signum.name)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        installed.append(signum)
    return installed


async def run_forever() -> None:
    from app.postgres_monitoring import PostgresWorkerMonitor

    settings = WorkerLoopSettings.from_environment()
    worker = build_worker_from_environment(
        claim_keepalive_seconds=(
            settings.stale_processing_seconds / 3
            if settings.stale_processing_seconds > 0
            else None
        )
    )
    monitor = PostgresWorkerMonitor()
    worker_name = "outlook-invoice-worker"

    def report_heartbeat(**values: object) -> None:
        try:
            monitor.heartbeat(worker_name, **values)  # type: ignore[arg-type]
        except Exception as error:
            # Monitoring must never prevent the worker from processing invoices.
            logger.warning("Outlook worker heartbeat failed: %s", error)

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event, asyncio.current_task())
    report_heartbeat(status="starting")
    logger.info(
        "Outlook invoice worker started (poll %.1fs, SharePoint scan %.1fs, "
        "concurrency %d, stale reclaim %.0fs).",
        settings.poll_seconds,
        settings.sharepoint_scan_seconds,
        worker.concurrency,
        settings.stale_processing_seconds,
    )
    await run_worker_loop(
        worker,
        settings,
        stop_event=stop_event,
        report_heartbeat=report_heartbeat,
    )


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main() -> None:
    load_project_environment()
    configure_logging()
    try:
        asyncio.run(run_forever())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Outlook invoice worker cancelled.")


if __name__ == "__main__":
    main()
