"""Activity feed and its server-sent-events stream."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.auth import ROLE_APPROVER_1, ROLE_APPROVER_2, User, get_current_user
from app.deps import invoice_is_visible_to_user

router = APIRouter()


@router.get("/api/activity")
def activity(
    http_request: Request,
    since_id: int = Query(0, ge=0), user: User = Depends(get_current_user)
) -> list[dict[str, object]]:
    app = http_request.app
    page_size = 50
    events = app.state.activity_feed.list_since(since_id, page_size)
    if user.role in {ROLE_APPROVER_1, ROLE_APPROVER_2}:
        # Filter page by page until something is visible or the feed runs
        # out: the UI only moves its cursor past a non-empty response, so a
        # full page of other approvers' events must not end the scan.
        visible_by_invoice: dict[int, bool] = {}
        visible_events = []
        while True:
            for event in events:
                if event.invoice_id is None:
                    continue
                if event.invoice_id not in visible_by_invoice:
                    invoice = app.state.invoice_store.get(event.invoice_id)
                    visible_by_invoice[event.invoice_id] = (
                        invoice is not None
                        and invoice_is_visible_to_user(invoice, user)
                    )
                if visible_by_invoice[event.invoice_id]:
                    visible_events.append(event)
            if visible_events or len(events) < page_size:
                break
            events = app.state.activity_feed.list_since(events[-1].id, page_size)
        events = visible_events
    return [asdict(event) for event in events]


@router.get("/api/activity/latest")
def activity_latest(
    http_request: Request, user: User = Depends(get_current_user)
) -> dict[str, int]:
    """Return only the newest activity event id.

    The UI uses this to position its activity cursor on page load without
    replaying history as toasts (``/api/activity`` with ``since_id=0``
    returns the *oldest* events). Only an integer is exposed -- the same
    value the SSE stream already signals to every role.
    """
    del user
    return {"latest_id": int(http_request.app.state.activity_feed.latest_id())}


STREAM_TICK_SECONDS = 0.5


async def _latest_activity_id(app) -> int:
    """Newest event id, looked up at most once per tick however many browsers
    are connected, so a faster tick does not multiply database queries."""
    cached = getattr(app.state, "_latest_activity_cache", None)
    now = time.monotonic()
    if cached is not None and now - cached[0] < STREAM_TICK_SECONDS * 0.8:
        return cached[1]
    value = await asyncio.to_thread(app.state.activity_feed.latest_id)
    app.state._latest_activity_cache = (now, value)
    return value


@router.get("/api/activity/stream")
async def activity_stream(
    request: Request,
    since_id: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    app = request.app
    del user

    async def events():
        cursor = since_id
        heartbeat_ticks = 0
        while not await request.is_disconnected():
            latest = await _latest_activity_id(app)
            if latest > cursor:
                cursor = latest
                yield f"event: invoice-update\ndata: {cursor}\n\n"
                heartbeat_ticks = 0
            else:
                heartbeat_ticks += 1
                if heartbeat_ticks >= 120:
                    yield ": keep-alive\n\n"
                    heartbeat_ticks = 0
            await asyncio.sleep(STREAM_TICK_SECONDS)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
