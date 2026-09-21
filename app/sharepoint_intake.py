from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Callable, Protocol

from app.activity_feed import ActivityFeedStore, ROLE_PURCHASE_LEDGER
from app.company_folders import REJECTED_INVOICES_FOLDER
from app.invoice_lifecycle import InvoiceExtractionUnavailableError
from app.invoices import InvoiceRecord
from app.pdf_validation import InvalidPdfError, validate_pdf
from app.sharepoint import SharePointClient, SharePointError


logger = logging.getLogger(__name__)


class InvalidIncomingPdfError(SharePointError):
    """Raised when an Incoming DriveItem cannot be processed as a PDF."""


class InvoiceIntakeStore(Protocol):
    def add_from_outlook(
        self,
        *,
        message: dict[str, object],
        attachment: dict[str, object],
        stored_path: Path,
    ) -> InvoiceRecord: ...

    def get_by_sharepoint_item_id(self, item_id: str) -> InvoiceRecord | None: ...

    def get_by_source_attachment(
        self, message_id: str, attachment_id: str
    ) -> InvoiceRecord | None: ...

    def update_fields(self, invoice_id: int, **fields: object) -> InvoiceRecord: ...

    def list(self, limit: int = 100) -> list[InvoiceRecord]: ...


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
            try:
                if self.ingest_item(item) is not None:
                    ingested += 1
            except InvalidIncomingPdfError as error:
                self._handle_invalid_item(item, error)
            except Exception:
                logger.exception(
                    "SharePoint Incoming item %r could not be ingested; "
                    "continuing with the remaining items.",
                    item.get("id"),
                )
        return ingested

    def ingest_item(
        self,
        item: dict[str, object],
        *,
        source_message: dict[str, object] | None = None,
        source_attachment: dict[str, object] | None = None,
        content: bytes | None = None,
        event_type: str = "sharepoint_intake",
    ) -> InvoiceRecord | None:
        item_id = self._required_string(item, "id")
        existing = self.invoice_store.get_by_sharepoint_item_id(item_id)
        if existing is not None:
            if source_message is not None and source_attachment is not None:
                message_id = self._required_string(source_message, "id")
                attachment_id = self._required_string(source_attachment, "id")
                source_record = self.invoice_store.get_by_source_attachment(
                    message_id, attachment_id
                )
                if source_record is None:
                    self.invoice_store.update_fields(
                        existing.id,
                        message_id=message_id,
                        attachment_id=attachment_id,
                    )
                elif source_record.id != existing.id:
                    raise SharePointError(
                        "The Outlook attachment and SharePoint DriveItem are "
                        "already linked to different invoice records."
                    )
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
            raise InvalidIncomingPdfError(
                f"SharePoint Incoming item '{filename}' is not a usable PDF: {error}"
            ) from error

        self.cache_directory.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
        cached_path = self.cache_directory / f"{cache_key}-{Path(filename).name}"
        cached_path.write_bytes(pdf_content)

        message = dict(source_message or {})
        message.setdefault("id", f"sharepoint:{item_id}")
        message.setdefault("subject", "Invoice added to SharePoint Incoming")
        message.setdefault("receivedDateTime", item.get("createdDateTime"))
        attachment = dict(source_attachment or {})
        attachment.setdefault("id", item_id)
        attachment.setdefault("name", filename)
        attachment["size"] = item.get("size", len(pdf_content))
        attachment["contentType"] = "application/pdf"
        attachment["sharepoint_item_id"] = item_id
        attachment["sharepoint_web_url"] = self.client.get_item_web_url(item)
        record = self.invoice_store.add_from_outlook(
            message=message,
            attachment=attachment,
            stored_path=cached_path,
        )
        if event_type == "outlook_intake":
            duplicate = self._find_identical_pdf(record, pdf_content)
            if duplicate is not None:
                record = self.invoice_store.update_fields(
                    record.id,
                    status="Needs Review",
                    duplicate_of_invoice_id=duplicate.id,
                    review_reason=(
                        f"Possible duplicate of invoice #{duplicate.id} "
                        f"(IRJ {duplicate.irj_number or 'not yet assigned'}, "
                        f"status {duplicate.status}), matched on identical PDF "
                        "content. Purchase Ledger must confirm whether this is "
                        "a genuinely separate invoice."
                    ),
                )
                if self.activity_feed is not None:
                    self.activity_feed.add_event(
                        event_type="possible_duplicate",
                        target_role=ROLE_PURCHASE_LEDGER,
                        message=(
                            f"'{filename}' is byte-for-byte identical to invoice "
                            f"#{duplicate.id} and needs duplicate review."
                        ),
                        invoice_id=record.id,
                    )
                return record
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

    def _find_identical_pdf(
        self, invoice: InvoiceRecord, content: bytes
    ) -> InvoiceRecord | None:
        content_digest = hashlib.sha256(content).digest()
        for candidate in self.invoice_store.list(limit=500):
            if candidate.id == invoice.id:
                continue
            try:
                candidate_digest = hashlib.sha256(
                    Path(candidate.stored_path).read_bytes()
                ).digest()
            except OSError:
                continue
            if candidate_digest == content_digest:
                return candidate
        return None

    def _handle_invalid_item(
        self, item: dict[str, object], error: InvalidIncomingPdfError
    ) -> None:
        item_id = str(item.get("id") or "")
        filename = str(item.get("name") or "unknown.pdf")
        try:
            if item_id:
                self.client.move_to_folder(
                    item_id,
                    REJECTED_INVOICES_FOLDER,
                    filename,
                )
        except Exception:
            logger.exception(
                "Invalid SharePoint Incoming item %r could not be moved to Rejected.",
                item_id,
            )
        logger.warning("%s", error)
        if self.activity_feed is not None:
            self.activity_feed.add_event(
                event_type="sharepoint_intake_rejected",
                target_role=ROLE_PURCHASE_LEDGER,
                message=f"'{filename}' was rejected during intake: {error}",
            )

    @staticmethod
    def _required_string(data: dict[str, object], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SharePointError(
                f"SharePoint Incoming item is missing required '{key}'."
            )
        return value
