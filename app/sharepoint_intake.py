from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Protocol

from app.activity_feed import ActivityFeedStore, ROLE_PURCHASE_LEDGER
from app.invoice_lifecycle import InvoiceExtractionUnavailableError
from app.invoices import InvoiceRecord
from app.pdf_validation import InvalidPdfError, validate_pdf
from app.sharepoint import SharePointClient, SharePointError


class InvoiceIntakeStore(Protocol):
    def add_from_outlook(
        self,
        *,
        message: dict[str, object],
        attachment: dict[str, object],
        stored_path: Path,
    ) -> InvoiceRecord: ...

    def get_by_sharepoint_item_id(self, item_id: str) -> InvoiceRecord | None: ...

    def update_fields(self, invoice_id: int, **fields: object) -> InvoiceRecord: ...


class SharePointIncomingMonitor:
    """Registers each PDF found in SharePoint Incoming exactly once.

    SharePoint remains the canonical file location. The local path is a
    processing cache used by Document Intelligence and the inline PDF preview.
    """

    def __init__(
        self,
        client: SharePointClient,
        invoice_store: InvoiceIntakeStore,
        *,
        cache_directory: Path,
        extraction_runner: Callable[[int], object] | None = None,
        activity_feed: ActivityFeedStore | None = None,
    ) -> None:
        self.client = client
        self.invoice_store = invoice_store
        self.cache_directory = cache_directory
        self.extraction_runner = extraction_runner
        self.activity_feed = activity_feed

    def scan_once(self) -> int:
        ingested = 0
        for item in self.client.list_incoming_pdfs():
            if self.ingest_item(item) is not None:
                ingested += 1
        return ingested

    def ingest_item(
        self,
        item: dict[str, object],
        *,
        source_message: dict[str, object] | None = None,
        content: bytes | None = None,
        event_type: str = "sharepoint_intake",
    ) -> InvoiceRecord | None:
        item_id = self._required_string(item, "id")
        existing = self.invoice_store.get_by_sharepoint_item_id(item_id)
        if existing is not None:
            return None

        filename = self._required_string(item, "name")
        if not filename.lower().endswith(".pdf"):
            raise SharePointError(
                f"SharePoint Incoming item '{filename}' is not a PDF."
            )
        pdf_content = content if content is not None else self.client.download_item(item_id)
        try:
            validate_pdf(pdf_content)
        except InvalidPdfError as error:
            raise SharePointError(
                f"SharePoint Incoming item '{filename}' is not a usable PDF: {error}"
            ) from error

        self.cache_directory.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
        cached_path = self.cache_directory / f"{cache_key}-{Path(filename).name}"
        cached_path.write_bytes(pdf_content)

        message = dict(source_message or {})
        message["id"] = f"sharepoint:{item_id}"
        message.setdefault("subject", "Invoice added to SharePoint Incoming")
        message.setdefault("receivedDateTime", item.get("createdDateTime"))
        attachment: dict[str, object] = {
            "id": item_id,
            "name": filename,
            "size": item.get("size", len(pdf_content)),
            "contentType": "application/pdf",
        }
        record = self.invoice_store.add_from_outlook(
            message=message,
            attachment=attachment,
            stored_path=cached_path,
        )
        record = self.invoice_store.update_fields(
            record.id,
            sharepoint_item_id=item_id,
            sharepoint_web_url=self.client.get_item_web_url(item),
        )
        if self.activity_feed is not None:
            self.activity_feed.add_event(
                event_type=event_type,
                target_role=ROLE_PURCHASE_LEDGER,
                message=(
                    f"'{filename}' was registered from SharePoint Incoming "
                    "and is awaiting invoice review."
                ),
                invoice_id=record.id,
            )
        if (
            self.extraction_runner is not None
            and record.status == "Awaiting AI Extraction"
        ):
            try:
                self.extraction_runner(record.id)
            except InvoiceExtractionUnavailableError:
                # The DriveItem and register row are already durable. Leave the
                # invoice retryable instead of causing the producer to upload it again.
                pass
            return self.invoice_store.get_by_sharepoint_item_id(item_id)
        return record

    @staticmethod
    def _required_string(data: dict[str, object], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SharePointError(
                f"SharePoint Incoming item is missing required '{key}'."
            )
        return value
