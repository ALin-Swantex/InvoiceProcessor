from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
import msal


GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._ -]+")


class OutlookConfigurationError(RuntimeError):
    pass


class OutlookGraphError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutlookSettings:
    tenant_id: str
    client_id: str
    client_secret: str
    mailbox: str
    download_directory: Path
    max_pdf_bytes: int = 20 * 1024 * 1024

    def validate(self) -> None:
        values = {
            "OUTLOOK_MCP_TENANT_ID": self.tenant_id,
            "OUTLOOK_MCP_CLIENT_ID": self.client_id,
            "OUTLOOK_MCP_CLIENT_SECRET": self.client_secret,
            "OUTLOOK_MCP_MAILBOX": self.mailbox,
        }
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise OutlookConfigurationError(
                f"Missing required Outlook MCP settings: {', '.join(missing)}."
            )
        if self.max_pdf_bytes <= 0:
            raise OutlookConfigurationError(
                "OUTLOOK_MCP_MAX_PDF_BYTES must be greater than zero."
            )


class MsalTokenProvider:
    def __init__(self, settings: OutlookSettings) -> None:
        self.application = msal.ConfidentialClientApplication(
            settings.client_id,
            authority=f"https://login.microsoftonline.com/{settings.tenant_id}",
            client_credential=settings.client_secret,
        )

    def __call__(self) -> str:
        result = self.application.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        token = result.get("access_token")
        if not isinstance(token, str):
            description = result.get("error_description", "Unknown token error.")
            raise OutlookGraphError(f"Unable to obtain Graph token: {description}")
        return token


class OutlookGraphClient:
    def __init__(
        self,
        settings: OutlookSettings,
        *,
        token_provider: Callable[[], str] | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self.token_provider = token_provider or MsalTokenProvider(settings)
        self.http_client = http_client or httpx.Client(timeout=30.0)
        self.settings.download_directory.mkdir(parents=True, exist_ok=True)

    def list_invoice_emails(
        self, *, limit: int = 20, unread_only: bool = True
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise OutlookGraphError("Email limit must be between 1 and 100.")

        filters = ["hasAttachments eq true"]
        if unread_only:
            filters.append("isRead eq false")

        response = self._get(
            f"/users/{quote(self.settings.mailbox, safe='')}/messages",
            params={
                "$top": str(limit),
                "$select": (
                    "id,internetMessageId,subject,from,receivedDateTime,"
                    "hasAttachments,isRead,webLink"
                ),
                "$filter": " and ".join(filters),
            },
        )
        messages = response.json().get("value", [])
        if not isinstance(messages, list):
            raise OutlookGraphError("Graph returned an invalid messages response.")
        return messages

    def get_invoice_email(self, message_id: str) -> dict[str, Any]:
        response = self._get(
            (
                f"/users/{quote(self.settings.mailbox, safe='')}/messages/"
                f"{quote(message_id, safe='')}"
            ),
            params={
                "$select": (
                    "id,internetMessageId,subject,from,receivedDateTime,"
                    "hasAttachments,isRead,webLink"
                )
            },
        )
        message = response.json()
        if not isinstance(message, dict) or not message.get("id"):
            raise OutlookGraphError("Graph returned an invalid message response.")
        return message

    def list_pdf_attachments(self, message_id: str) -> list[dict[str, Any]]:
        response = self._get(
            (
                f"/users/{quote(self.settings.mailbox, safe='')}/messages/"
                f"{quote(message_id, safe='')}/attachments"
            ),
            params={"$select": "id,name,contentType,size,isInline"},
        )
        attachments = response.json().get("value", [])
        if not isinstance(attachments, list):
            raise OutlookGraphError("Graph returned an invalid attachments response.")

        return [
            attachment
            for attachment in attachments
            if isinstance(attachment, dict)
            and not attachment.get("isInline", False)
            and (
                str(attachment.get("contentType", "")).casefold()
                == "application/pdf"
                or str(attachment.get("name", "")).lower().endswith(".pdf")
            )
        ]

    def download_pdf_attachment(
        self, message_id: str, attachment_id: str, filename: str
    ) -> Path:
        safe_filename = self._safe_pdf_filename(filename)
        response = self._get(
            (
                f"/users/{quote(self.settings.mailbox, safe='')}/messages/"
                f"{quote(message_id, safe='')}/attachments/"
                f"{quote(attachment_id, safe='')}/$value"
            )
        )
        content = response.content
        if len(content) > self.settings.max_pdf_bytes:
            raise OutlookGraphError("The PDF exceeds the configured size limit.")
        if not content.startswith(b"%PDF-"):
            raise OutlookGraphError("The attachment content is not a valid PDF.")

        target = self._unique_target(safe_filename)
        target.write_bytes(content)
        return target

    def create_inbox_subscription(
        self,
        *,
        notification_url: str,
        client_state: str,
        lifecycle_notification_url: str | None = None,
    ) -> dict[str, Any]:
        self._validate_subscription_values(notification_url, client_state)
        payload: dict[str, Any] = {
            "changeType": "created",
            "notificationUrl": notification_url,
            "resource": (
                f"/users/{self.settings.mailbox}/"
                "mailFolders('inbox')/messages"
            ),
            "expirationDateTime": self._subscription_expiration(),
            "clientState": client_state,
            "latestSupportedTlsVersion": "v1_2",
        }
        if lifecycle_notification_url:
            if not lifecycle_notification_url.startswith("https://"):
                raise OutlookConfigurationError(
                    "The lifecycle notification URL must use HTTPS."
                )
            payload["lifecycleNotificationUrl"] = lifecycle_notification_url
        return self._send_json("POST", "/subscriptions", payload).json()

    def renew_subscription(self, subscription_id: str) -> dict[str, Any]:
        if not subscription_id.strip():
            raise OutlookConfigurationError("A subscription ID is required.")
        return self._send_json(
            "PATCH",
            f"/subscriptions/{quote(subscription_id, safe='')}",
            {"expirationDateTime": self._subscription_expiration()},
        ).json()

    def _get(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> httpx.Response:
        response = self.http_client.get(
            f"{GRAPH_BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self.token_provider()}"},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            request_id = response.headers.get("request-id", "not provided")
            raise OutlookGraphError(
                f"Microsoft Graph request failed with HTTP {response.status_code}; "
                f"request ID: {request_id}."
            ) from error
        return response

    def _send_json(
        self, method: str, path: str, payload: dict[str, Any]
    ) -> httpx.Response:
        response = self.http_client.request(
            method,
            f"{GRAPH_BASE_URL}{path}",
            json=payload,
            headers={
                "Authorization": f"Bearer {self.token_provider()}",
                "Content-Type": "application/json",
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            request_id = response.headers.get("request-id", "not provided")
            raise OutlookGraphError(
                f"Microsoft Graph request failed with HTTP {response.status_code}; "
                f"request ID: {request_id}."
            ) from error
        return response

    @staticmethod
    def _validate_subscription_values(
        notification_url: str, client_state: str
    ) -> None:
        if not notification_url.startswith("https://"):
            raise OutlookConfigurationError(
                "OUTLOOK_WEBHOOK_URL must be a publicly reachable HTTPS URL."
            )
        if not client_state or len(client_state) > 128:
            raise OutlookConfigurationError(
                "OUTLOOK_WEBHOOK_CLIENT_STATE must contain 1 to 128 characters."
            )

    @staticmethod
    def _subscription_expiration() -> str:
        # Outlook message subscriptions support just under seven days.
        expiration = datetime.now(timezone.utc) + timedelta(days=6, hours=23)
        return expiration.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _safe_pdf_filename(filename: str) -> str:
        name = Path(filename).name.strip()
        cleaned = SAFE_FILENAME_PATTERN.sub("_", name)
        if not cleaned.lower().endswith(".pdf"):
            raise OutlookGraphError("Only PDF attachments can be downloaded.")
        return cleaned[:180]

    def _unique_target(self, filename: str) -> Path:
        candidate = self.settings.download_directory / filename
        if not candidate.exists():
            return candidate

        stem = candidate.stem
        suffix = candidate.suffix
        counter = 2
        while True:
            candidate = self.settings.download_directory / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1


def settings_from_environment() -> OutlookSettings:
    return OutlookSettings(
        tenant_id=os.environ.get("OUTLOOK_MCP_TENANT_ID", ""),
        client_id=os.environ.get("OUTLOOK_MCP_CLIENT_ID", ""),
        client_secret=os.environ.get("OUTLOOK_MCP_CLIENT_SECRET", ""),
        mailbox=os.environ.get("OUTLOOK_MCP_MAILBOX", ""),
        download_directory=Path(
            os.environ.get("OUTLOOK_MCP_DOWNLOAD_DIR", "outlook_downloads")
        ),
        max_pdf_bytes=int(
            os.environ.get("OUTLOOK_MCP_MAX_PDF_BYTES", str(20 * 1024 * 1024))
        ),
    )


def graph_client_from_environment() -> OutlookGraphClient:
    return OutlookGraphClient(settings_from_environment())
