"""Activity feed and its server-sent-events stream."""

from __future__ import annotations

import asyncio
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
    events = app.state.activity_feed.list_since(since_id)
    if user.role in {ROLE_APPROVER_1, ROLE_APPROVER_2}:
        visible_events = []
        for event in events:
            if event.invoice_id is None:
                continue
            invoice = app.state.invoice_store.get(event.invoice_id)
            if (
                invoice is not None
                and invoice_is_visible_to_user(invoice, user)
            ):
                visible_events.append(event)
        events = visible_events
    return [asdict(event) for event in events]


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
            latest = await asyncio.to_thread(
                app.state.activity_feed.latest_id
            )
            if latest > cursor:
                cursor = latest
                yield f"event: invoice-update\ndata: {cursor}\n\n"
                heartbeat_ticks = 0
            else:
                heartbeat_ticks += 1
                if heartbeat_ticks >= 30:
                    yield ": keep-alive\n\n"
                    heartbeat_ticks = 0
            await asyncio.sleep(2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
