from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.invoices import InvoiceStore
from app.outlook_notifications import OutlookNotificationStore
from app.outlook_worker import (
    CANCELLED_CLAIM_NOTE,
    OutlookInvoiceWorker,
    WorkerLoopSettings,
    install_signal_handlers,
    run_worker_loop,
)
from tests.pdf_helpers import VALID_PDF_BYTES


def enqueue(store: OutlookNotificationStore, message_id: str = "message-1") -> None:
    store.enqueue(
        subscription_id="subscription-1",
        message_id=message_id,
        resource=f"Users/mailbox/Messages/{message_id}",
        change_type="created",
        payload={"resourceData": {"id": message_id}},
    )


class ManyAttachmentRetriever:
    def __init__(self, directory: Path, count: int) -> None:
        self.directory = directory
        self.count = count
        self.in_flight = 0
        self.max_in_flight = 0

    async def list_invoice_emails(self, **_: object) -> list[dict[str, object]]:
        return []

    async def get_invoice_email(self, message_id: str) -> dict[str, object]:
        return {"id": message_id, "subject": "Invoices"}

    async def list_invoice_attachments(self, message_id: str) -> list[dict[str, object]]:
        return [
            {"id": f"attachment-{index}", "name": f"invoice-{index}.pdf"}
            for index in range(self.count)
        ]

    async def download_invoice_attachment(
        self, message_id: str, attachment_id: str, filename: str
    ) -> str:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        path = self.directory / f"{attachment_id}.pdf"
        path.write_bytes(VALID_PDF_BYTES)
        return str(path)


def test_attachments_download_concurrently_within_the_configured_bound(
    tmp_path: Path,
) -> None:
    notifications = OutlookNotificationStore(tmp_path / "notifications.db")
    enqueue(notifications)
    invoices = InvoiceStore(tmp_path / "invoices.db")
    retriever = ManyAttachmentRetriever(tmp_path, count=7)
    worker = OutlookInvoiceWorker(notifications, invoices, retriever, concurrency=3)

    assert asyncio.run(worker.process_next()) is True

    assert retriever.max_in_flight == 3
    registered = sorted(record.attachment_id for record in invoices.list())
    assert registered == sorted(f"attachment-{index}" for index in range(7))
    assert len(notifications.list(status="completed")) == 1


def test_concurrency_defaults_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OUTLOOK_WORKER_CONCURRENCY", "1")
    notifications = OutlookNotificationStore(tmp_path / "notifications.db")
    enqueue(notifications)
    retriever = ManyAttachmentRetriever(tmp_path, count=3)
    worker = OutlookInvoiceWorker(
        notifications, InvoiceStore(tmp_path / "invoices.db"), retriever
    )

    asyncio.run(worker.process_next())

    assert worker.concurrency == 1
    assert retriever.max_in_flight == 1


class OverlapRetriever(ManyAttachmentRetriever):
    """Deadlocks unless the message and attachment list are fetched together."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory, count=1)
        self.message_started = asyncio.Event()
        self.attachments_started = asyncio.Event()

    async def get_invoice_email(self, message_id: str) -> dict[str, object]:
        self.message_started.set()
        await asyncio.wait_for(self.attachments_started.wait(), timeout=2)
        return {"id": message_id}

    async def list_invoice_attachments(self, message_id: str) -> list[dict[str, object]]:
        self.attachments_started.set()
        await asyncio.wait_for(self.message_started.wait(), timeout=2)
        return await super().list_invoice_attachments(message_id)


def test_message_and_attachment_list_are_fetched_in_parallel(tmp_path: Path) -> None:
    notifications = OutlookNotificationStore(tmp_path / "notifications.db")
    enqueue(notifications)

    async def scenario() -> bool:
        worker = OutlookInvoiceWorker(
            notifications,
            InvoiceStore(tmp_path / "invoices.db"),
            OverlapRetriever(tmp_path),
        )
        return await worker.process_next()

    assert asyncio.run(scenario()) is True


class BlockingRetriever(ManyAttachmentRetriever):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory, count=1)
        self.download_started = asyncio.Event()

    async def download_invoice_attachment(
        self, message_id: str, attachment_id: str, filename: str
    ) -> str:
        self.download_started.set()
        await asyncio.Event().wait()  # never finishes
        raise AssertionError("unreachable")


def test_cancelled_item_is_released_back_to_pending(tmp_path: Path) -> None:
    notifications = OutlookNotificationStore(tmp_path / "notifications.db")
    enqueue(notifications)

    async def scenario() -> None:
        retriever = BlockingRetriever(tmp_path)
        worker = OutlookInvoiceWorker(
            notifications, InvoiceStore(tmp_path / "invoices.db"), retriever
        )
        task = asyncio.create_task(worker.process_next())
        await retriever.download_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert notifications.list(status="processing") == []
    pending = notifications.list(status="pending")
    assert len(pending) == 1
    assert pending[0].last_error == CANCELLED_CLAIM_NOTE


class FakeLoopWorker:
    """Duck-typed worker used to drive run_worker_loop deterministically."""

    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.queue_items = 0
        self.claims = 0
        self.scans = 0
        self.reminders = 0
        self.reclaims: list[float] = []
        self.polled_since: list[datetime | None] = []
        self.stuck_checks = 0
        self.on_process = None

    def reclaim_stale_notifications(self, older_than_seconds: float) -> int:
        self.reclaims.append(older_than_seconds)
        return 0

    async def process_next(self) -> bool:
        self.clock[0] += 1.0
        if self.queue_items <= 0:
            return False
        self.queue_items -= 1
        self.claims += 1
        if self.on_process is not None:
            self.on_process()
        return True

    async def enqueue_from_mailbox(
        self, limit: int, *, received_since: datetime | None = None
    ) -> int:
        self.polled_since.append(received_since)
        return 0

    async def retry_stuck_extractions(self, older_than_seconds: float) -> int:
        self.stuck_checks += 1
        return 0

    async def scan_sharepoint_incoming(self) -> int:
        self.scans += 1
        return 0

    async def send_scheduled_notifications(self) -> int:
        self.reminders += 1
        return 0


def test_sharepoint_scan_runs_on_its_own_interval_not_every_tick() -> None:
    clock = [0.0]
    worker = FakeLoopWorker(clock)
    worker.queue_items = 20  # busy: 20 back-to-back items, 1 simulated second each
    settings = WorkerLoopSettings(
        poll_seconds=0,
        sharepoint_scan_seconds=10,
        reminder_scan_seconds=1000,
    )

    async def scenario() -> None:
        stop = asyncio.Event()

        def stop_when_drained() -> None:
            if worker.queue_items == 0:
                stop.set()

        worker.on_process = stop_when_drained
        await run_worker_loop(worker, settings, stop_event=stop, clock=lambda: clock[0])

    asyncio.run(scenario())

    assert worker.claims == 20
    # First pass scans immediately, then once per 10 simulated seconds.
    assert worker.scans == 2
    assert worker.reminders == 1


def test_sharepoint_scan_does_not_run_on_every_idle_tick() -> None:
    clock = [0.0]
    worker = FakeLoopWorker(clock)  # empty queue: every pass is an idle tick
    settings = WorkerLoopSettings(
        poll_seconds=0,
        sharepoint_scan_seconds=10,
        reminder_scan_seconds=1000,
    )
    ticks = 0

    async def scenario() -> None:
        nonlocal ticks
        stop = asyncio.Event()
        original = worker.process_next

        async def counting_process_next() -> bool:
            nonlocal ticks
            ticks += 1
            if ticks >= 25:
                stop.set()
            return await original()

        worker.process_next = counting_process_next  # type: ignore[method-assign]
        await run_worker_loop(worker, settings, stop_event=stop, clock=lambda: clock[0])

    asyncio.run(scenario())

    assert ticks == 25
    # Scans at t=1, 11, 21 only - not on each of the 25 idle ticks.
    assert worker.scans == 3


def test_stop_request_finishes_current_item_and_claims_nothing_more(
    tmp_path: Path,
) -> None:
    notifications = OutlookNotificationStore(tmp_path / "notifications.db")
    enqueue(notifications, "message-1")
    enqueue(notifications, "message-2")
    invoices = InvoiceStore(tmp_path / "invoices.db")
    heartbeats: list[dict[str, object]] = []

    async def scenario() -> None:
        stop = asyncio.Event()
        retriever = ManyAttachmentRetriever(tmp_path, count=1)
        original = retriever.download_invoice_attachment

        async def download_then_signal(*args: str) -> str:
            stop.set()  # SIGTERM arrives while the first item is mid-flight
            return await original(*args)

        retriever.download_invoice_attachment = download_then_signal  # type: ignore[method-assign]
        worker = OutlookInvoiceWorker(notifications, invoices, retriever)
        await asyncio.wait_for(
            run_worker_loop(
                worker,
                WorkerLoopSettings(poll_seconds=60),
                stop_event=stop,
                report_heartbeat=lambda **values: heartbeats.append(values),
            ),
            timeout=5,
        )

    asyncio.run(scenario())

    completed = notifications.list(status="completed")
    assert [item.message_id for item in completed] == ["message-1"]
    assert [item.message_id for item in notifications.list(status="pending")] == [
        "message-2"
    ]
    assert notifications.list(status="processing") == []
    assert heartbeats[-1] == {"status": "stopped"}


def test_idle_loop_wakes_immediately_on_stop() -> None:
    clock = [0.0]
    worker = FakeLoopWorker(clock)

    async def scenario() -> None:
        stop = asyncio.Event()
        loop_task = asyncio.create_task(
            run_worker_loop(worker, WorkerLoopSettings(poll_seconds=3600), stop_event=stop)
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=1)

    asyncio.run(scenario())
    assert worker.claims == 0
    assert worker.reclaims == [900.0]


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="POSIX signals only")
def test_sigterm_sets_the_stop_event() -> None:
    async def scenario() -> bool:
        stop = asyncio.Event()
        installed = install_signal_handlers(stop)
        if signal.SIGTERM not in installed:
            pytest.skip("signal handlers unavailable in this event loop")
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), timeout=2)
        for signum in installed:
            asyncio.get_running_loop().remove_signal_handler(signum)
        return stop.is_set()

    assert asyncio.run(scenario()) is True


def _set_claimed_at(database: Path, notification_id: int, moment: datetime) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE outlook_notifications "
            "SET payload = json_set(payload, '$.claimed_at', ?) WHERE id = ?",
            (moment.isoformat(timespec="microseconds"), notification_id),
        )


def test_stale_processing_items_are_reclaimed(tmp_path: Path) -> None:
    database = tmp_path / "notifications.db"
    store = OutlookNotificationStore(database)
    enqueue(store, "stale")
    enqueue(store, "fresh")
    stale = store.claim_next()
    fresh = store.claim_next()
    assert stale is not None and fresh is not None
    _set_claimed_at(database, stale.id, datetime.now(timezone.utc) - timedelta(hours=1))

    assert store.reclaim_stale(900) == 1

    pending = store.list(status="pending")
    assert [item.message_id for item in pending] == ["stale"]
    assert "Reclaimed" in str(pending[0].last_error)
    assert [item.message_id for item in store.list(status="processing")] == ["fresh"]

    # A reclaimed item can be claimed again, and its attempts keep counting.
    again = store.claim_next()
    assert again is not None and again.message_id == "stale" and again.attempts == 2


def test_touch_claim_keeps_long_running_item_from_being_reclaimed(tmp_path: Path) -> None:
    database = tmp_path / "notifications.db"
    store = OutlookNotificationStore(database)
    enqueue(store)
    claimed = store.claim_next()
    assert claimed is not None
    _set_claimed_at(database, claimed.id, datetime.now(timezone.utc) - timedelta(hours=1))

    store.touch_claim(claimed.id)

    assert store.reclaim_stale(900) == 0
    assert len(store.list(status="processing")) == 1


def test_reclaim_marks_items_failed_after_max_attempts(tmp_path: Path) -> None:
    database = tmp_path / "notifications.db"
    store = OutlookNotificationStore(database)
    enqueue(store)
    claimed = store.claim_next()
    assert claimed is not None
    _set_claimed_at(database, claimed.id, datetime.now(timezone.utc) - timedelta(hours=1))

    assert store.reclaim_stale(900, max_attempts=1) == 1

    failed = store.list(status="failed")
    assert len(failed) == 1 and "Reclaimed" in str(failed[0].last_error)


def test_legacy_claim_without_timestamp_falls_back_to_received_at(tmp_path: Path) -> None:
    database = tmp_path / "notifications.db"
    store = OutlookNotificationStore(database)
    enqueue(store)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE outlook_notifications SET status = 'processing', received_at = ?",
            (old,),
        )

    assert store.reclaim_stale(900) == 1
    assert len(store.list(status="pending")) == 1


def _idle_loop(worker: FakeLoopWorker, settings: WorkerLoopSettings, ticks: int) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        original = worker.process_next
        count = 0

        async def counting_process_next() -> bool:
            nonlocal count
            count += 1
            if count >= ticks:
                stop.set()
            return await original()

        worker.process_next = counting_process_next  # type: ignore[method-assign]
        await run_worker_loop(
            worker, settings, stop_event=stop, clock=lambda: worker.clock[0]
        )

    asyncio.run(scenario())


def test_first_mailbox_poll_catches_up_on_days_the_worker_was_down() -> None:
    worker = FakeLoopWorker([0.0])
    settings = WorkerLoopSettings(
        poll_seconds=0, local_polling_enabled=True, local_polling_catch_up_days=3
    )

    _idle_loop(worker, settings, ticks=4)

    first, *later = worker.polled_since
    assert first is not None
    assert datetime.now(timezone.utc) - first >= timedelta(days=3)
    assert later and all(since is None for since in later)  # then the usual window


def test_stuck_extraction_sweep_runs_on_its_own_interval() -> None:
    worker = FakeLoopWorker([0.0])
    settings = WorkerLoopSettings(poll_seconds=0, stuck_extraction_check_seconds=10)

    _idle_loop(worker, settings, ticks=25)  # one simulated second per tick

    assert worker.stuck_checks == 3  # t=1, 11, 21


def test_invoices_stuck_in_extraction_are_retried(tmp_path: Path) -> None:
    store = InvoiceStore(tmp_path / "invoices.db")
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(VALID_PDF_BYTES)
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    stuck = store.add_from_outlook(
        message={"id": "message-1"},
        attachment={"id": "attachment-1", "name": "invoice.pdf", "size": 10},
        stored_path=pdf,
        created_at=old,
    )
    fresh = store.add_from_outlook(
        message={"id": "message-2"},
        attachment={"id": "attachment-2", "name": "invoice.pdf", "size": 10},
        stored_path=pdf,
    )
    done = store.add_from_outlook(
        message={"id": "message-3"},
        attachment={"id": "attachment-3", "name": "invoice.pdf", "size": 10},
        stored_path=pdf,
        created_at=old,
    )
    store.update_fields(done.id, status="Needs Review")
    retried: list[int] = []

    def runner(invoice_id: int) -> None:
        retried.append(invoice_id)
        if invoice_id == stuck.id:
            raise RuntimeError("network still down")  # logged, tried again later

    worker = OutlookInvoiceWorker(
        OutlookNotificationStore(tmp_path / "notifications.db"),
        store,
        ManyAttachmentRetriever(tmp_path, 0),
        runner,
    )

    count = asyncio.run(worker.retry_stuck_extractions(900))

    assert count == 1
    assert retried == [stuck.id]  # not the fresh one (may be mid-extraction)
    assert fresh.id not in retried and done.id not in retried
