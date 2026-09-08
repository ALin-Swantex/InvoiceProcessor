"""Central invoice metadata store backed by a SharePoint List, instead of
the local SQLite database in app/invoices.py.

WHY THIS FILE EXISTS
---------------------
The workflow state for every invoice (status, IRJ number, approver
decisions, PO matching notes, payment/reconciliation info) was originally
kept in a local SQLite file (runtime_data/invoices.db). That works well for
a single-process prototype, but it means the "source of truth" lives only
on one machine's disk.

This module re-implements the exact same public interface as
app.invoices.InvoiceStore (add_from_outlook, list, list_by_status, get,
update_status, update_sharepoint, update_fields) but persists every field as
a row (a SharePoint "list item") in a SharePoint List via the Microsoft
Graph API, so the metadata lives centrally in Microsoft 365 alongside the
PDF documents themselves.

Because both classes share the same public method signatures and both
return app.invoices.InvoiceRecord instances, app/main.py and
app/invoice_lifecycle.py do not need to change at all to use this store --
only the object that gets constructed and injected into create_app()
changes (see INVOICE_STORE_BACKEND in app/main.py).

*** PLACEHOLDERS -- NOT YET CONFIGURED ***
The following environment variables are placeholders because there is no
SharePoint site/list access yet. Nothing will work until these point at a
real, provisioned SharePoint List:

  SHAREPOINT_INVOICES_SITE_ID   e.g. "contoso.sharepoint.com,<site-guid>,<web-guid>"
  SHAREPOINT_INVOICES_LIST_ID   the GUID of the "Invoices" SharePoint List

The SharePoint List itself must be created with one column per field on
InvoiceRecord (see FIELD_COLUMNS below for the expected *internal* column
names -- these are the names used in the Graph API request/response body,
which can differ from the human-readable display name shown in the
SharePoint UI). When the real list is provisioned, either:
  (a) name its columns to match FIELD_COLUMNS exactly, or
  (b) edit FIELD_COLUMNS below to match the real internal names.

KNOWN DESIGN LIMITATION -- IRJ NUMBERING
IRJ number generation (app/irj.py) still uses a small local SQLite counter
file, not this store. Microsoft Graph does not provide an atomic
increment/lock primitive for list items, so a naive "read highest number,
add one, write" against a SharePoint List is subject to race conditions
under concurrent requests. If IRJ numbering also needs to be centralised,
the safest options are an Azure Function with a durable/atomic counter, or
Dataverse's built-in autonumber column -- not a simple SharePoint List item.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

import httpx

from app.invoices import InvoiceRecord
from app.outlook_graph import MsalTokenProvider, OutlookSettings


GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# Maps InvoiceRecord field name -> SharePoint List internal column name.
# PLACEHOLDER: assumes the list is provisioned with internal column names
# identical to these keys. Update the values on the right if your real
# SharePoint List uses different internal names.
FIELD_COLUMNS: dict[str, str] = {
    "message_id": "message_id",
    "attachment_id": "attachment_id",
    "internet_message_id": "internet_message_id",
    "sender_name": "sender_name",
    "sender_address": "sender_address",
    "subject": "subject",
    "received_at": "received_at",
    "original_filename": "original_filename",
    "stored_path": "stored_path",
    "size_bytes": "size_bytes",
    "status": "status",
    "created_at": "created_at",
    "sharepoint_item_id": "sharepoint_item_id",
    "sharepoint_web_url": "sharepoint_web_url",
    "irj_number": "irj_number",
    "company": "company",
    "supplier": "supplier",
    "supplier_invoice_number": "supplier_invoice_number",
    "po_number": "po_number",
    "invoice_type": "invoice_type",
    "invoice_date": "invoice_date",
    "invoice_value": "invoice_value",
    "currency": "currency",
    "ai_confidence": "ai_confidence",
    "ai_field_confidences": "ai_field_confidences",
    "ai_review_warnings": "ai_review_warnings",
    "approver1_name": "approver1_name",
    "approver1_email": "approver1_email",
    "approver1_decision": "approver1_decision",
    "approver1_date": "approver1_date",
    "approver1_comments": "approver1_comments",
    "approver2_name": "approver2_name",
    "approver2_email": "approver2_email",
    "approver2_decision": "approver2_decision",
    "approver2_date": "approver2_date",
    "approver2_comments": "approver2_comments",
    "po_query_notes": "po_query_notes",
    "po_query_category": "po_query_category",
    "po_query_contact": "po_query_contact",
    "payment_date": "payment_date",
    "supplier_account_number": "supplier_account_number",
    "payment_reference": "payment_reference",
    "payment_method": "payment_method",
    "paid_by": "paid_by",
    "reconciliation_date": "reconciliation_date",
    "reconciliation_notes": "reconciliation_notes",
    "reconciled_by": "reconciled_by",
    "rejection_reason": "rejection_reason",
    "review_reason": "review_reason",
    "review_return_status": "review_return_status",
    "duplicate_of_invoice_id": "duplicate_of_invoice_id",
    "hold_reason": "hold_reason",
    "hold_level": "hold_level",
    "sage_registered_at": "sage_registered_at",
    "sage_reference": "sage_reference",
    "sage_registered_by": "sage_registered_by",
    "is_foreign_payment": "is_foreign_payment",
    "payment_route_decided_at": "payment_route_decided_at",
    "payment_route_decided_by": "payment_route_decided_by",
    "foreign_allocation_date": "foreign_allocation_date",
    "foreign_allocation_reference": "foreign_allocation_reference",
    "foreign_allocated_by": "foreign_allocated_by",
    "cancelled_at": "cancelled_at",
    "cancelled_by": "cancelled_by",
    "cancellation_reason": "cancellation_reason",
}


class SharePointListConfigurationError(RuntimeError):
    pass


class SharePointListError(RuntimeError):
    pass


@dataclass(frozen=True)
class SharePointListSettings:
    site_id: str
    list_id: str

    def validate(self) -> None:
        values = {
            "SHAREPOINT_INVOICES_SITE_ID": self.site_id,
            "SHAREPOINT_INVOICES_LIST_ID": self.list_id,
        }
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise SharePointListConfigurationError(
                f"Missing required SharePoint List settings: {', '.join(missing)}. "
                "These are placeholders until a real 'Invoices' SharePoint List "
                "is provisioned -- see app/sharepoint_invoice_store.py."
            )


def sharepoint_list_settings_from_environment() -> SharePointListSettings:
    return SharePointListSettings(
        site_id=os.environ.get("SHAREPOINT_INVOICES_SITE_ID", ""),
        list_id=os.environ.get("SHAREPOINT_INVOICES_LIST_ID", ""),
    )


class SharePointInvoiceStore:
    """Drop-in replacement for app.invoices.InvoiceStore that persists every
    invoice's workflow metadata as an item in a SharePoint List instead of a
    local SQLite database.

    Every method mirrors InvoiceStore's public API exactly (same names,
    arguments, and return types) so it can be substituted with no changes
    required in app/main.py or app/invoice_lifecycle.py.
    """

    def __init__(
        self,
        settings: SharePointListSettings,
        outlook_settings: OutlookSettings,
        *,
        token_provider: Callable[[], str] | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self.token_provider = token_provider or MsalTokenProvider(outlook_settings)
        self.http_client = http_client or httpx.Client(timeout=60.0)

    # ------------------------------------------------------------------
    # Public API -- mirrors app.invoices.InvoiceStore
    # ------------------------------------------------------------------

    def add_from_outlook(
        self,
        *,
        message: dict[str, object],
        attachment: dict[str, object],
        stored_path: Any,
    ) -> InvoiceRecord:
        message_id = self._required_string(message, "id")
        attachment_id = self._required_string(attachment, "id")
        filename = self._required_string(attachment, "name")
        sender_name, sender_address = self._sender(message)

        existing = self._find_by_message_and_attachment(message_id, attachment_id)
        if existing is not None:
            if (
                existing.sharepoint_item_id is None
                and self._optional_string(attachment.get("sharepoint_item_id"))
            ):
                self._update_item_fields(
                    existing.id,
                    self._to_sharepoint_fields(
                        {
                            "stored_path": str(stored_path),
                            "sharepoint_item_id": attachment["sharepoint_item_id"],
                            "sharepoint_web_url": self._optional_string(
                                attachment.get("sharepoint_web_url")
                            ),
                        }
                    ),
                )
                refreshed = self.get(existing.id)
                if refreshed is None:
                    raise RuntimeError(
                        f"Invoice {existing.id} could not be read after linking."
                    )
                return refreshed
            return existing

        from datetime import datetime, timezone

        created_at = datetime.now(timezone.utc).isoformat()
        fields = {
            "message_id": message_id,
            "attachment_id": attachment_id,
            "internet_message_id": self._optional_string(
                message.get("internetMessageId")
            ),
            "sender_name": sender_name,
            "sender_address": sender_address,
            "subject": self._optional_string(message.get("subject")),
            "received_at": self._optional_string(message.get("receivedDateTime")),
            "original_filename": filename,
            "stored_path": str(stored_path),
            "size_bytes": self._optional_int(attachment.get("size")),
            "status": "Awaiting AI Extraction",
            "created_at": created_at,
            "sharepoint_item_id": self._optional_string(
                attachment.get("sharepoint_item_id")
            ),
            "sharepoint_web_url": self._optional_string(
                attachment.get("sharepoint_web_url")
            ),
        }
        item = self._create_item(fields)
        return self._item_to_record(item)

    def list(self, limit: int = 100) -> list[InvoiceRecord]:
        if limit < 1 or limit > 500:
            raise ValueError("Invoice limit must be between 1 and 500.")
        items = self._list_items(top=limit)
        records = [self._item_to_record(item) for item in items]
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records[:limit]

    def list_by_status(
        self, statuses: list[str], limit: int = 200
    ) -> list[InvoiceRecord]:
        if not statuses:
            return []
        records = [
            record for record in self.list(limit=limit) if record.status in statuses
        ]
        return records[:limit]

    def get(self, invoice_id: int) -> InvoiceRecord | None:
        item = self._get_item(invoice_id)
        return self._item_to_record(item) if item is not None else None

    def get_by_sharepoint_item_id(self, item_id: str) -> InvoiceRecord | None:
        for record in self.list(limit=500):
            if record.sharepoint_item_id == item_id:
                return record
        return None

    def get_by_source_attachment(
        self, message_id: str, attachment_id: str
    ) -> InvoiceRecord | None:
        return self._find_by_message_and_attachment(message_id, attachment_id)

    def update_status(self, invoice_id: int, status: str) -> None:
        self.update_fields(invoice_id, status=status)

    def update_sharepoint(
        self,
        invoice_id: int,
        *,
        sharepoint_item_id: str,
        sharepoint_web_url: str | None,
        status: str,
    ) -> None:
        """Record the SharePoint *drive* item ID/URL of the filed PDF
        (distinct from the SharePoint *list* item ID used as this
        InvoiceRecord's own `id`), and update the processing status."""
        self.update_fields(
            invoice_id,
            sharepoint_item_id=sharepoint_item_id,
            sharepoint_web_url=sharepoint_web_url,
            status=status,
        )

    def update_fields(self, invoice_id: int, **fields: object) -> InvoiceRecord:
        allowed = set(InvoiceRecord.__dataclass_fields__) - {"id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown invoice fields: {', '.join(sorted(unknown))}.")
        if not fields:
            record = self.get(invoice_id)
            if record is None:
                raise KeyError(f"Invoice {invoice_id} was not found.")
            return record

        sp_fields = {FIELD_COLUMNS[name]: value for name, value in fields.items()}
        self._update_item_fields(invoice_id, sp_fields)
        record = self.get(invoice_id)
        if record is None:
            raise KeyError(f"Invoice {invoice_id} was not found.")
        return record

    # ------------------------------------------------------------------
    # Graph API calls against /sites/{site-id}/lists/{list-id}
    # ------------------------------------------------------------------

    def _list_base_url(self) -> str:
        return (
            f"{GRAPH_BASE_URL}/sites/{quote(self.settings.site_id, safe='')}/"
            f"lists/{quote(self.settings.list_id, safe='')}"
        )

    def _create_item(self, fields: dict[str, object]) -> dict[str, Any]:
        sp_fields = {FIELD_COLUMNS[name]: value for name, value in fields.items()}
        url = f"{self._list_base_url()}/items"
        response = self._send_json("POST", url, {"fields": sp_fields})
        return response.json()

    def _list_items(self, *, top: int) -> list[dict[str, Any]]:
        """Fetch list items with their field values.

        PLACEHOLDER LIMITATION: this only follows @odata.nextLink pages
        while more items are needed to satisfy `top`; for very large lists
        a real deployment should push status/date filters into the Graph
        $filter query instead of paging through everything client-side.
        """
        url = f"{self._list_base_url()}/items"
        params: dict[str, Any] | None = {
            "$expand": "fields",
            "$top": min(top, 200),
        }
        items: list[dict[str, Any]] = []
        while url and len(items) < top:
            response = self.http_client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.token_provider()}"},
            )
            self._raise_for_status(response, "list items")
            payload = response.json()
            items.extend(payload.get("value", []))
            url = payload.get("@odata.nextLink")
            params = None  # nextLink already includes all query params
        return items

    def _get_item(self, invoice_id: int) -> dict[str, Any] | None:
        url = f"{self._list_base_url()}/items/{invoice_id}"
        response = self.http_client.get(
            url,
            params={"$expand": "fields"},
            headers={"Authorization": f"Bearer {self.token_provider()}"},
        )
        if response.status_code == 404:
            return None
        self._raise_for_status(response, "get item")
        return response.json()

    def _find_by_message_and_attachment(
        self, message_id: str, attachment_id: str
    ) -> InvoiceRecord | None:
        """Look up an existing item by the (message_id, attachment_id)
        natural key, emulating SQLite's UNIQUE + INSERT OR IGNORE.

        PLACEHOLDER LIMITATION: Graph $filter on list item fields requires
        the message_id/attachment_id columns to be indexed on the real
        SharePoint List, otherwise this call will fail or be throttled.
        """
        url = f"{self._list_base_url()}/items"
        filter_query = (
            f"fields/message_id eq '{message_id}' and "
            f"fields/attachment_id eq '{attachment_id}'"
        )
        response = self.http_client.get(
            url,
            params={"$expand": "fields", "$filter": filter_query},
            headers={"Authorization": f"Bearer {self.token_provider()}"},
        )
        self._raise_for_status(response, "find existing item")
        matches = response.json().get("value", [])
        if not matches:
            return None
        return self._item_to_record(matches[0])

    def _update_item_fields(self, invoice_id: int, sp_fields: dict[str, Any]) -> None:
        url = f"{self._list_base_url()}/items/{invoice_id}/fields"
        response = self._send_json("PATCH", url, sp_fields)
        if response.status_code == 404:
            raise KeyError(f"Invoice {invoice_id} was not found.")

    def _send_json(self, method: str, url: str, payload: dict[str, Any]) -> httpx.Response:
        response = self.http_client.request(
            method,
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {self.token_provider()}",
                "Content-Type": "application/json",
            },
        )
        self._raise_for_status(response, f"{method} {url}")
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response, action: str) -> None:
        if response.status_code >= 400 and response.status_code != 404:
            request_id = response.headers.get("request-id", "not provided")
            raise SharePointListError(
                f"SharePoint List {action} failed with HTTP "
                f"{response.status_code}; request ID: {request_id}."
            )

    # ------------------------------------------------------------------
    # Conversion helpers
    # ------------------------------------------------------------------

    def _item_to_record(self, item: dict[str, Any]) -> InvoiceRecord:
        raw_fields = item.get("fields", {})
        values: dict[str, Any] = {"id": int(item["id"])}
        for python_name, column_name in FIELD_COLUMNS.items():
            values[python_name] = raw_fields.get(column_name)
        return InvoiceRecord(**values)

    @staticmethod
    def _required_string(data: dict[str, object], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Outlook data is missing required field '{key}'.")
        return value

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _optional_int(value: object) -> int | None:
        return value if isinstance(value, int) else None

    @classmethod
    def _sender(cls, message: dict[str, object]) -> tuple[str | None, str | None]:
        sender = message.get("from")
        if not isinstance(sender, dict):
            return None, None
        email_address = sender.get("emailAddress")
        if not isinstance(email_address, dict):
            return None, None
        return (
            cls._optional_string(email_address.get("name")),
            cls._optional_string(email_address.get("address")),
        )


def sharepoint_invoice_store_from_environment() -> SharePointInvoiceStore:
    from app.outlook_graph import settings_from_environment

    return SharePointInvoiceStore(
        sharepoint_list_settings_from_environment(),
        settings_from_environment(),
    )
