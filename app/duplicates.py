from __future__ import annotations

from dataclasses import dataclass

from app.invoices import InvoiceRecord, InvoiceStore


# ---------------------------------------------------------------------------
# Duplicate detection.
#
# GENERAL_PROCESS.md section 7 ("Check for Duplicates") defines the
# suggested key as Company + Supplier + Supplier Invoice Number, with
# invoice value and date as secondary evidence. A possible duplicate must be
# placed in "Needs Review"; it must NEVER be automatically deleted or
# silently merged -- a human always makes the final call.
#
# That primary key relies on Purchase Ledger having typed a supplier
# invoice number in at confirmation time. AI extraction is currently a
# placeholder (nothing is auto-filled), so it's easy to confirm an invoice
# with that field left blank -- in which case the primary key check has
# nothing reliable to compare and is skipped. To avoid silently missing an
# obvious duplicate in that situation (e.g. the exact same PDF confirmed
# twice, or the same source email attachment processed twice), a secondary
# check compares the underlying source file identity instead: same company
# plus either (a) the same source email message/attachment (Outlook
# re-delivered or re-processed the same email), or (b) the same original
# filename and file size (the same PDF manually re-added, or re-fetched
# under a new invoice row).
# ---------------------------------------------------------------------------

# Statuses that represent an invoice still actively moving through the
# workflow, or one that has finished it. Rejected invoices are intentionally
# still checked against -- a duplicate of a previously rejected invoice is
# exactly the kind of thing Purchase Ledger should be told about.
_COMPARABLE_STATUSES_EXCLUDED = set()  # every existing invoice is compared


@dataclass(frozen=True)
class DuplicateMatch:
    invoice_id: int
    irj_number: str | None
    status: str
    invoice_value: float | None
    invoice_date: str | None
    match_basis: str = "supplier invoice number"


def find_possible_duplicate(
    invoice_store: InvoiceStore,
    *,
    exclude_invoice_id: int,
    company: str,
    supplier: str,
    supplier_invoice_number: str | None,
) -> DuplicateMatch | None:
    """Return the first existing invoice that shares the duplicate key
    (company + supplier + supplier invoice number), if any, excluding the
    invoice currently being confirmed.

    When supplier_invoice_number is blank, falls back to matching on the
    source file identity (same email message/attachment, or same filename
    + size) within the same company, so a duplicate is still caught even
    when there is no invoice number to key on.
    """
    company_key = company.strip().casefold()
    supplier_key = supplier.strip().casefold()

    key_number = (supplier_invoice_number or "").strip().casefold()
    if key_number:
        for candidate in invoice_store.list(limit=500):
            if candidate.id == exclude_invoice_id:
                continue
            if (candidate.company or "").strip().casefold() != company_key:
                continue
            if (candidate.supplier or "").strip().casefold() != supplier_key:
                continue
            if (candidate.supplier_invoice_number or "").strip().casefold() != key_number:
                continue
            return DuplicateMatch(
                invoice_id=candidate.id,
                irj_number=candidate.irj_number,
                status=candidate.status,
                invoice_value=candidate.invoice_value,
                invoice_date=candidate.invoice_date,
                match_basis="supplier invoice number",
            )
        return None

    return _find_duplicate_by_source_file(
        invoice_store,
        exclude_invoice_id=exclude_invoice_id,
        company_key=company_key,
    )


def _find_duplicate_by_source_file(
    invoice_store: InvoiceStore,
    *,
    exclude_invoice_id: int,
    company_key: str,
) -> DuplicateMatch | None:
    invoice = invoice_store.get(exclude_invoice_id)
    if invoice is None:
        return None

    filename_key = (invoice.original_filename or "").strip().casefold()

    for candidate in invoice_store.list(limit=500):
        if candidate.id == exclude_invoice_id:
            continue
        if (candidate.company or "").strip().casefold() != company_key:
            continue

        # Same source email attachment -- the strongest possible signal
        # that this is a re-processed duplicate rather than a coincidence.
        if (
            invoice.internet_message_id
            and candidate.internet_message_id == invoice.internet_message_id
            and candidate.attachment_id == invoice.attachment_id
        ):
            return DuplicateMatch(
                invoice_id=candidate.id,
                irj_number=candidate.irj_number,
                status=candidate.status,
                invoice_value=candidate.invoice_value,
                invoice_date=candidate.invoice_date,
                match_basis="source email attachment",
            )

        # Same filename and file size -- a strong proxy for "the same PDF"
        # when there's no supplier invoice number to compare.
        if (
            filename_key
            and (candidate.original_filename or "").strip().casefold() == filename_key
            and candidate.size_bytes is not None
            and candidate.size_bytes == invoice.size_bytes
        ):
            return DuplicateMatch(
                invoice_id=candidate.id,
                irj_number=candidate.irj_number,
                status=candidate.status,
                invoice_value=candidate.invoice_value,
                invoice_date=candidate.invoice_date,
                match_basis="matching filename and file size",
            )
    return None
