from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable
from urllib.parse import quote

import httpx

from app.graph_http import GraphSession
from app.outlook_graph import (
    OutlookSettings,
    default_token_provider,
    settings_from_environment,
)


class EmailNotificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmailNotificationSettings:
    sender_mailbox: str
    enabled: bool

    @classmethod
    def from_environment(cls) -> EmailNotificationSettings:
        enabled = os.environ.get(
            "EMAIL_NOTIFICATIONS_ENABLED", "false"
        ).strip().casefold() in {"1", "true", "yes", "on"}
        sender = os.environ.get(
            "EMAIL_NOTIFICATION_SENDER",
            os.environ.get("GRAPH_MAILBOX", ""),
        ).strip()
        if enabled and not sender:
            raise EmailNotificationError(
                "EMAIL_NOTIFICATION_SENDER or GRAPH_MAILBOX is required "
                "when email notifications are enabled."
            )
        return cls(sender_mailbox=sender, enabled=enabled)


class GraphEmailNotifier:
    def __init__(
        self,
        settings: EmailNotificationSettings,
        outlook_settings: OutlookSettings,
        *,
        token_provider: Callable[[], str] | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.token_provider = token_provider or default_token_provider(outlook_settings)
        self.http_client = http_client or httpx.Client(timeout=30.0)
        self.session = GraphSession(
            self.http_client,
            self.token_provider,
            error_class=EmailNotificationError,
        )

    def send(self, *, recipient: str, subject: str, body: str) -> None:
        if not self.settings.enabled:
            return
        recipient = recipient.strip()
        if not recipient:
            raise EmailNotificationError(
                "An email notification recipient is required."
            )
        mailbox = quote(self.settings.sender_mailbox, safe="")
        # sendMail is not idempotent: GraphSession retries it only on HTTP 429
        # (throttled before execution) or when the connection never opened,
        # so a retry can never deliver the same email twice.
        response = self.session.request(
            "POST",
            f"/users/{mailbox}/sendMail",
            idempotent=False,
            raise_for_status=False,
            json={
                "message": {
                    "subject": subject,
                    "body": {
                        "contentType": "Text",
                        "content": body,
                    },
                    "toRecipients": [
                        {
                            "emailAddress": {
                                "address": recipient,
                            }
                        }
                    ],
                },
                "saveToSentItems": True,
            },
        )
        if response.status_code != 202:
            self.session.raise_error(response, "Microsoft Graph email delivery")


def send_email_notification(*, recipient: str, subject: str, body: str) -> bool:
    settings = EmailNotificationSettings.from_environment()
    if not settings.enabled:
        return False
    notifier = _notifier_for(settings, settings_from_environment())
    notifier.send(recipient=recipient, subject=subject, body=body)
    return True


@lru_cache(maxsize=4)
def _notifier_for(
    settings: EmailNotificationSettings,
    outlook_settings: OutlookSettings,
) -> GraphEmailNotifier:
    return GraphEmailNotifier(settings, outlook_settings)
