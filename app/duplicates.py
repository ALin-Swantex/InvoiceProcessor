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

    Returns None when supplier_invoice_number is blank -- there is nothing
    reliable to key on, so duplicate detection is skipped for that invoice
    rather than guessing.
    """
    key_number = (supplier_invoice_number or "").strip().casefold()
    if not key_number:
        return None

    company_key = company.strip().casefold()
    supplier_key = supplier.strip().casefold()

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
        )
    return None
