"""Microsoft Graph (Outlook) webhook and subscription lifecycle endpoints."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from app.outlook_notifications import extract_message_id

router = APIRouter()


@router.post("/api/outlook/notifications")
async def outlook_notifications(
    request: Request,
    validation_token: str | None = Query(None, alias="validationToken"),
) -> Response:
    app = request.app
    if validation_token is not None:
        return PlainTextResponse(validation_token)

    expected_client_state = str(app.state.webhook_client_state)
    if not expected_client_state:
        raise HTTPException(
            status_code=503,
            detail="Outlook webhook client state is not configured.",
        )

    payload = await request.json()
    notifications = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(notifications, list):
        raise HTTPException(
            status_code=400, detail="Invalid Microsoft Graph notification payload."
        )

    for notification in notifications:
        if not isinstance(notification, dict):
            continue
        received_client_state = notification.get("clientState")
        if not isinstance(received_client_state, str) or not secrets.compare_digest(
            received_client_state, expected_client_state
        ):
            raise HTTPException(
                status_code=401, detail="Invalid notification client state."
            )

        message_id = extract_message_id(notification)
        subscription_id = notification.get("subscriptionId")
        resource = notification.get("resource")
        change_type = notification.get("changeType")
        if not all(
            isinstance(value, str) and value
            for value in (
                message_id,
                subscription_id,
                resource,
                change_type,
            )
        ):
            raise HTTPException(
                status_code=400,
                detail="Notification is missing required message data.",
            )

        app.state.notification_store.enqueue(
            subscription_id=subscription_id,
            message_id=message_id,
            resource=resource,
            change_type=change_type,
            payload=notification,
        )

    return Response(status_code=202)


@router.post("/api/outlook/lifecycle")
async def outlook_lifecycle(
    request: Request,
    validation_token: str | None = Query(None, alias="validationToken"),
) -> Response:
    app = request.app
    if validation_token is not None:
        return PlainTextResponse(validation_token)

    expected_client_state = str(app.state.webhook_client_state)
    if not expected_client_state:
        raise HTTPException(
            status_code=503,
            detail="Outlook webhook client state is not configured.",
        )
    payload = await request.json()
    notifications = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(notifications, list):
        raise HTTPException(
            status_code=400, detail="Invalid lifecycle notification payload."
        )
    for notification in notifications:
        if not isinstance(notification, dict):
            continue
        received_client_state = notification.get("clientState")
        if not isinstance(received_client_state, str) or not secrets.compare_digest(
            received_client_state, expected_client_state
        ):
            raise HTTPException(
                status_code=401,
                detail="Invalid lifecycle notification client state.",
            )
    # Lifecycle persistence and alerting are added with production monitoring.
    return Response(status_code=202)
