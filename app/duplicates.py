from __future__ import annotations

from dataclasses import dataclass

from app.invoices import InvoiceRecord, InvoiceStore


# ---------------------------------------------------------------------------
# Duplicate detection.
#
# The suggested key is Company + Supplier + Supplier Invoice Number, with
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


def duplicate_reference(invoice_id: int, irj_number: str | None) -> str:
    """Return the user-facing reference for an existing duplicate invoice."""
    normalized_irj = (irj_number or "").strip()
    if normalized_irj:
        return f"IRJ {normalized_irj}"
    return f"invoice #{invoice_id} (IRJ not yet assigned)"


def find_possible_duplicate(
    invoice_store: InvoiceStore,
    *,
    exclude_invoice_id: int,
    company: str,
    supplier: str,
    supplier_invoice_number: str | None,
) -> DuplicateMatch | None:
    """Return the newest existing invoice that shares the duplicate key
    (company + supplier + supplier invoice number), if any, excluding the
    invoice currently being confirmed.

    When supplier_invoice_number is blank, falls back to matching on the
    source file identity (same email message/attachment, or same filename
    + size) within the same company, so a duplicate is still caught even
    when there is no invoice number to key on.

    Both checks are indexed queries over every invoice, not a scan of the
    newest few hundred.
    """
    if (supplier_invoice_number or "").strip():
        candidate = invoice_store.find_duplicate_by_number(
            exclude_id=exclude_invoice_id,
            company=company,
            supplier=supplier,
            number=supplier_invoice_number or "",
        )
        return _match(candidate, "supplier invoice number")

    invoice = invoice_store.get(exclude_invoice_id)
    if invoice is None:
        return None
    candidate = invoice_store.find_duplicate_by_source(invoice, company)
    if candidate is None:
        return None
    # Same source email attachment is the strongest signal that this is a
    # re-processed duplicate rather than a coincidence.
    if (
        invoice.internet_message_id
        and candidate.internet_message_id == invoice.internet_message_id
        and candidate.attachment_id == invoice.attachment_id
    ):
        return _match(candidate, "source email attachment")
    return _match(candidate, "matching filename and file size")


def _match(candidate: InvoiceRecord | None, basis: str) -> DuplicateMatch | None:
    if candidate is None:
        return None
    return DuplicateMatch(
        invoice_id=candidate.id,
        irj_number=candidate.irj_number,
        status=candidate.status,
        invoice_value=candidate.invoice_value,
        invoice_date=candidate.invoice_date,
        match_basis=basis,
    )
