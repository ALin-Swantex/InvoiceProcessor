"""Microsoft Graph (Outlook) webhook and subscription lifecycle endpoints."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from app.activity_feed import ROLE_PURCHASE_LEDGER
from app.outlook_graph import graph_client_from_environment
from app.outlook_notifications import extract_message_id

logger = logging.getLogger("invoice_processor")

router = APIRouter()


async def _json_body(request: Request) -> object:
    try:
        return await request.json()
    except ValueError as error:
        # JSONDecodeError and UnicodeDecodeError are both ValueErrors.
        raise HTTPException(
            status_code=400, detail="The request body is not valid JSON."
        ) from error


async def _graph_batch(
    request: Request, invalid_payload: str
) -> tuple[str, list[object]]:
    """The configured client state and the batch's ``value`` list."""
    expected_client_state = str(request.app.state.webhook_client_state)
    if not expected_client_state:
        raise HTTPException(
            status_code=503,
            detail="Outlook webhook client state is not configured.",
        )
    payload = await _json_body(request)
    notifications = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(notifications, list):
        raise HTTPException(status_code=400, detail=invalid_payload)
    return expected_client_state, notifications


def _check_client_state(
    notification: dict[str, object], expected: str, invalid_detail: str
) -> None:
    received = notification.get("clientState")
    if not isinstance(received, str) or not secrets.compare_digest(received, expected):
        raise HTTPException(status_code=401, detail=invalid_detail)


@router.post("/api/outlook/notifications")
async def outlook_notifications(
    request: Request,
    validation_token: str | None = Query(None, alias="validationToken"),
) -> Response:
    app = request.app
    if validation_token is not None:
        return PlainTextResponse(validation_token)

    expected_client_state, notifications = await _graph_batch(
        request, "Invalid Microsoft Graph notification payload."
    )

    # Validate the whole batch before enqueuing anything: a rejected batch
    # is retried by Graph, so a partial enqueue would be queued twice.
    validated = []
    for notification in notifications:
        if not isinstance(notification, dict):
            continue
        _check_client_state(
            notification, expected_client_state, "Invalid notification client state."
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

        validated.append(
            (subscription_id, message_id, resource, change_type, notification)
        )

    for subscription_id, message_id, resource, change_type, notification in validated:
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
    background_tasks: BackgroundTasks,
    validation_token: str | None = Query(None, alias="validationToken"),
) -> Response:
    if validation_token is not None:
        return PlainTextResponse(validation_token)

    expected_client_state, notifications = await _graph_batch(
        request, "Invalid lifecycle notification payload."
    )
    for notification in notifications:
        if not isinstance(notification, dict):
            continue
        _check_client_state(
            notification,
            expected_client_state,
            "Invalid lifecycle notification client state.",
        )
    for notification in notifications:
        if isinstance(notification, dict):
            # After the 202: a renewal is a Graph call, and Microsoft expects
            # a prompt answer.
            background_tasks.add_task(_handle_lifecycle_event, request.app, notification)
    return Response(status_code=202)


def _report_subscription(app: FastAPI, event_type: str, message: str) -> None:
    if event_type == "outlook_subscription_problem":
        logger.error("%s", message)
    else:
        logger.info("%s", message)
    app.state.activity_feed.add_event(
        event_type=event_type,
        target_role=ROLE_PURCHASE_LEDGER,
        message=message,
    )


def _handle_lifecycle_event(app: FastAPI, notification: dict[str, object]) -> None:
    """Act on a Graph lifecycle event. Without this, a subscription that
    Microsoft removes or stops sending to fails silently: invoice emails simply
    stop arriving."""
    event = notification.get("lifecycleEvent")
    subscription_id = str(notification.get("subscriptionId") or "unknown")
    if event == "reauthorizationRequired":
        try:
            graph_client_from_environment().renew_subscription(subscription_id)
        except Exception as error:
            _report_subscription(
                app,
                "outlook_subscription_problem",
                f"The Outlook subscription {subscription_id} needs reauthorising "
                f"and could not be renewed automatically ({error}). New invoice "
                "emails may stop arriving. Renew it with: python -m "
                f"app.outlook_subscription renew {subscription_id}",
            )
            return
        _report_subscription(
            app,
            "outlook_subscription_renewed",
            f"The Outlook subscription {subscription_id} was renewed after "
            "Microsoft asked for reauthorisation.",
        )
    elif event == "subscriptionRemoved":
        _report_subscription(
            app,
            "outlook_subscription_problem",
            f"Microsoft removed the Outlook subscription {subscription_id}, so new "
            "invoice emails are not being received. Create a new one with: "
            "python -m app.outlook_subscription create",
        )
    elif event == "missed":
        _report_subscription(
            app,
            "outlook_subscription_problem",
            f"Microsoft reports missed Outlook notifications for subscription "
            f"{subscription_id}; some invoice emails may not have been queued. "
            "Check the inbox for invoices that are not in the app.",
        )
