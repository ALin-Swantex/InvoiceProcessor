from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

from app.graph_http import (
    GRAPH_BASE_URL as GRAPH_BASE_URL,  # re-exported for existing importers
    GraphSession,
    MsalClientCredentialTokenProvider,
    shared_token_provider,
)


SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._ -]+")
PDF_EXTENSIONS = {".pdf"}
EXCEL_EXTENSIONS = {".xls", ".xlsx"}
SUPPORTED_INVOICE_EXTENSIONS = PDF_EXTENSIONS | EXCEL_EXTENSIONS


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
    max_excel_bytes: int = 50 * 1024 * 1024
    excel_conversion_drive_id: str = ""
    excel_conversion_folder: str = "Invoice Conversion"

    def validate(self) -> None:
        values = {
            "GRAPH_TENANT_ID": self.tenant_id,
            "GRAPH_CLIENT_ID": self.client_id,
            "GRAPH_CLIENT_SECRET": self.client_secret,
            "GRAPH_MAILBOX": self.mailbox,
        }
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise OutlookConfigurationError(
                f"Missing required Outlook MCP settings: {', '.join(missing)}."
            )
        if self.max_pdf_bytes <= 0:
            raise OutlookConfigurationError(
                "OUTLOOK_MAX_PDF_BYTES must be greater than zero."
            )
        if self.max_excel_bytes <= 0:
            raise OutlookConfigurationError(
                "OUTLOOK_MAX_EXCEL_BYTES must be greater than zero."
            )


class MsalTokenProvider(MsalClientCredentialTokenProvider):
    def __init__(self, settings: OutlookSettings) -> None:
        super().__init__(
            settings.tenant_id,
            settings.client_id,
            settings.client_secret,
            error_factory=OutlookGraphError,
        )


def default_token_provider(settings: OutlookSettings) -> Callable[[], str]:
    """One process-wide token provider per app registration.

    Outlook, SharePoint and email notifications all authenticate with the same
    GRAPH_* app registration, so they share one MSAL token cache.
    """
    return shared_token_provider(
        settings.tenant_id,
        settings.client_id,
        settings.client_secret,
        OutlookGraphError,
    )


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
        self.token_provider = token_provider or default_token_provider(settings)
        self.http_client = http_client or httpx.Client(timeout=30.0)
        self.session = GraphSession(
            self.http_client,
            self.token_provider,
            error_class=OutlookGraphError,
        )
        self.settings.download_directory.mkdir(parents=True, exist_ok=True)

    def list_invoice_emails(
        self,
        *,
        limit: int = 20,
        unread_only: bool = True,
        received_since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise OutlookGraphError("Email limit must be between 1 and 100.")

        filters: list[str] = []
        if received_since is not None:
            if received_since.utcoffset() is None:
                raise OutlookGraphError("The Outlook received-since time must include a timezone.")
            received_utc = received_since.astimezone(timezone.utc)
            filters.append(
                "receivedDateTime ge "
                f"{received_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            )
        filters.append("hasAttachments eq true")
        if unread_only:
            filters.append("isRead eq false")

        response = self._get(
            f"/users/{quote(self.settings.mailbox, safe='')}/mailFolders('inbox')/messages",
            params={
                "$top": str(limit),
                "$select": (
                    "id,internetMessageId,subject,from,receivedDateTime,"
                    "hasAttachments,isRead,webLink"
                ),
                "$filter": " and ".join(filters),
                "$orderby": "receivedDateTime desc",
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

    def list_invoice_attachments(self, message_id: str) -> list[dict[str, Any]]:
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
            and Path(str(attachment.get("name", ""))).suffix.casefold()
            in SUPPORTED_INVOICE_EXTENSIONS
        ]

    def list_pdf_attachments(self, message_id: str) -> list[dict[str, Any]]:
        """Return the legacy PDF-only subset of supported attachments."""
        return [
            attachment
            for attachment in self.list_invoice_attachments(message_id)
            if Path(str(attachment.get("name", ""))).suffix.casefold()
            in PDF_EXTENSIONS
        ]

    def download_invoice_attachment(
        self, message_id: str, attachment_id: str, filename: str
    ) -> Path:
        suffix = Path(filename).suffix.casefold()
        if suffix in PDF_EXTENSIONS:
            return self.download_pdf_attachment(message_id, attachment_id, filename)
        if suffix in EXCEL_EXTENSIONS:
            return self._download_excel_as_pdf(message_id, attachment_id, filename)
        raise OutlookGraphError(
            "Only PDF, XLS, and XLSX invoice attachments are supported."
        )

    def download_pdf_attachment(
        self, message_id: str, attachment_id: str, filename: str
    ) -> Path:
        safe_filename = self._safe_pdf_filename(filename)
        content = self.session.download(
            self._attachment_value_path(message_id, attachment_id),
            max_bytes=self.settings.max_pdf_bytes,
            too_large_error=lambda: OutlookGraphError(
                "The PDF exceeds the configured size limit."
            ),
        )
        if not content.startswith(b"%PDF-"):
            raise OutlookGraphError("The attachment content is not a valid PDF.")

        return self._write_unique(safe_filename, content)

    def _download_excel_as_pdf(
        self, message_id: str, attachment_id: str, filename: str
    ) -> Path:
        drive_id = self.settings.excel_conversion_drive_id.strip()
        if not drive_id:
            raise OutlookConfigurationError(
                "EXCEL_CONVERSION_DRIVE_ID (or SHAREPOINT_DRIVE_ID) is required "
                "to convert Excel invoice attachments to PDF."
            )

        safe_source_name = self._safe_excel_filename(filename)
        source_content = self.session.download(
            self._attachment_value_path(message_id, attachment_id),
            max_bytes=self.settings.max_excel_bytes,
            too_large_error=lambda: OutlookGraphError(
                "The Excel attachment exceeds the configured size limit."
            ),
        )
        if not source_content:
            raise OutlookGraphError("The Excel attachment is empty.")

        temp_name = f"{uuid.uuid4().hex}-{safe_source_name}"
        folder = self.settings.excel_conversion_folder.strip().strip("/")
        remote_path = f"{folder}/{temp_name}" if folder else temp_name
        uploaded = self._put_graph_content(
            (
                f"/drives/{quote(drive_id, safe='')}/root:/"
                f"{quote(remote_path, safe='/')}:/content"
            ),
            source_content,
        ).json()
        item_id = uploaded.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise OutlookGraphError(
                "Microsoft Graph did not return an item ID for the temporary workbook."
            )

        conversion_error: Exception | None = None
        pdf_content: bytes | None = None
        try:
            pdf_content = self.session.download(
                (
                    f"/drives/{quote(drive_id, safe='')}/items/"
                    f"{quote(item_id, safe='')}/content"
                ),
                params={"format": "pdf"},
                follow_redirects=True,
                max_bytes=self.settings.max_pdf_bytes,
                too_large_error=lambda: OutlookGraphError(
                    "The converted PDF exceeds the configured size limit."
                ),
            )
            if not pdf_content.startswith(b"%PDF-"):
                raise OutlookGraphError(
                    "Microsoft Graph did not return a valid PDF conversion."
                )
        except Exception as error:
            conversion_error = error

        try:
            self._delete_graph_item(
                f"/drives/{quote(drive_id, safe='')}/items/"
                f"{quote(item_id, safe='')}"
            )
        except Exception as cleanup_error:
            if conversion_error is not None:
                raise OutlookGraphError(
                    f"{conversion_error} Temporary workbook cleanup also failed: "
                    f"{cleanup_error}"
                ) from conversion_error
            raise

        if conversion_error is not None:
            raise conversion_error
        assert pdf_content is not None

        return self._write_unique(
            self._safe_pdf_filename(f"{Path(safe_source_name).stem}.pdf"),
            pdf_content,
        )

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
        # Not retried on 5xx/timeouts: a lost success would create a second
        # subscription.
        return self._send_json(
            "POST", "/subscriptions", payload, idempotent=False
        ).json()

    def renew_subscription(self, subscription_id: str) -> dict[str, Any]:
        if not subscription_id.strip():
            raise OutlookConfigurationError("A subscription ID is required.")
        return self._send_json(
            "PATCH",
            f"/subscriptions/{quote(subscription_id, safe='')}",
            {"expirationDateTime": self._subscription_expiration()},
            idempotent=True,
        ).json()

    def _attachment_value_path(self, message_id: str, attachment_id: str) -> str:
        return (
            f"/users/{quote(self.settings.mailbox, safe='')}/messages/"
            f"{quote(message_id, safe='')}/attachments/"
            f"{quote(attachment_id, safe='')}/$value"
        )

    def _get(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> httpx.Response:
        return self.session.request("GET", path, params=params)

    def _send_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        *,
        idempotent: bool = False,
    ) -> httpx.Response:
        return self.session.request(
            method, path, json=payload, idempotent=idempotent
        )

    def _put_graph_content(
        self, path: str, content: bytes
    ) -> httpx.Response:
        # Only used for the uniquely named temporary workbook, so a retried
        # PUT simply rewrites the same file.
        return self.session.request(
            "PUT",
            path,
            content=content,
            headers={"Content-Type": "application/octet-stream"},
            idempotent=True,
        )

    def _delete_graph_item(self, path: str) -> None:
        self.session.request("DELETE", path, ok_on_retry_statuses={404})

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

    @staticmethod
    def _safe_excel_filename(filename: str) -> str:
        name = Path(filename).name.strip()
        cleaned = SAFE_FILENAME_PATTERN.sub("_", name)
        if Path(cleaned).suffix.casefold() not in EXCEL_EXTENSIONS:
            raise OutlookGraphError("Only XLS and XLSX files can be converted.")
        return cleaned[:180]

    def _write_unique(self, filename: str, content: bytes) -> Path:
        """Atomically claim a free filename and write content to it.

        Uses exclusive creation so concurrent downloads of attachments with
        the same name never overwrite each other.
        """
        directory = self.settings.download_directory
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        counter = 1
        while True:
            name = filename if counter == 1 else f"{stem}_{counter}{suffix}"
            candidate = directory / name
            try:
                with open(candidate, "xb") as handle:
                    handle.write(content)
            except FileExistsError:
                counter += 1
                continue
            return candidate


def settings_from_environment() -> OutlookSettings:
    return OutlookSettings(
        tenant_id=os.environ.get("GRAPH_TENANT_ID", ""),
        client_id=os.environ.get("GRAPH_CLIENT_ID", ""),
        client_secret=os.environ.get("GRAPH_CLIENT_SECRET", ""),
        mailbox=os.environ.get("GRAPH_MAILBOX", ""),
        download_directory=Path(
            os.environ.get("OUTLOOK_DOWNLOAD_DIR", "outlook_downloads")
        ),
        max_pdf_bytes=int(
            os.environ.get("OUTLOOK_MAX_PDF_BYTES", str(20 * 1024 * 1024))
        ),
        max_excel_bytes=int(
            os.environ.get("OUTLOOK_MAX_EXCEL_BYTES", str(50 * 1024 * 1024))
        ),
        excel_conversion_drive_id=os.environ.get(
            "EXCEL_CONVERSION_DRIVE_ID",
            os.environ.get("SHAREPOINT_DRIVE_ID", ""),
        ),
        excel_conversion_folder=os.environ.get(
            "EXCEL_CONVERSION_TEMP_FOLDER", "Invoice Conversion"
        ),
    )


def graph_client_from_environment() -> OutlookGraphClient:
    return OutlookGraphClient(settings_from_environment())
