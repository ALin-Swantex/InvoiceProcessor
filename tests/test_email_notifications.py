from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.email_notifications import (
    EmailNotificationError,
    EmailNotificationSettings,
    GraphEmailNotifier,
    send_email_notification,
)
from app.outlook_graph import OutlookSettings


def outlook_settings(tmp_path: Path) -> OutlookSettings:
    return OutlookSettings(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        mailbox="testinboundinvoices@swantex.com",
        download_directory=tmp_path,
    )


def test_graph_email_notification_uses_configured_mailbox_and_recipient(
    tmp_path: Path,
) -> None:
    request: httpx.Request | None = None

    def respond(incoming: httpx.Request) -> httpx.Response:
        nonlocal request
        request = incoming
        return httpx.Response(202)

    notifier = GraphEmailNotifier(
        EmailNotificationSettings(
            sender_mailbox="testinboundinvoices@swantex.com",
            enabled=True,
        ),
        outlook_settings(tmp_path),
        token_provider=lambda: "graph-token",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    notifier.send(
        recipient="ALin@swantex.com",
        subject="New invoice (invoice.pdf) waiting for approval",
        body="New invoice (invoice.pdf) is waiting for your approval.",
    )

    assert request is not None
    assert request.url.path.endswith(
        "/users/testinboundinvoices@swantex.com/sendMail"
    )
    assert request.headers["authorization"] == "Bearer graph-token"
    payload = __import__("json").loads(request.content)
    assert payload["message"]["toRecipients"][0]["emailAddress"]["address"] == (
        "ALin@swantex.com"
    )
    assert payload["message"]["subject"] == (
        "New invoice (invoice.pdf) waiting for approval"
    )
    assert payload["saveToSentItems"] is True


def test_graph_email_notification_surfaces_delivery_failure(
    tmp_path: Path,
) -> None:
    notifier = GraphEmailNotifier(
        EmailNotificationSettings(
            sender_mailbox="testinboundinvoices@swantex.com",
            enabled=True,
        ),
        outlook_settings(tmp_path),
        token_provider=lambda: "graph-token",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    403,
                    headers={"request-id": "request-123"},
                )
            )
        ),
    )

    with pytest.raises(
        EmailNotificationError,
        match="HTTP 403; request ID: request-123",
    ):
        notifier.send(
            recipient="ALin@swantex.com",
            subject="Test",
            body="Test",
        )


def test_disabled_email_notification_reports_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMAIL_NOTIFICATIONS_ENABLED", "false")

    assert send_email_notification(
        recipient="approver@example.test",
        subject="Test",
        body="Test",
    ) is False
