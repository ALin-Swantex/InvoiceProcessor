from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


SAFE_REFERENCE = re.compile(r"[^A-Za-z0-9._-]+")


class RoutingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ConfirmedInvoice:
    invoice_id: str
    company: str
    company_folder: str
    original_filename: str
    irj_number: str
    purchase_order_number: str | None = None
    po_matching_folder: str | None = None
    purchase_ledger_recipient: str | None = None


@dataclass(frozen=True)
class RoutingDecision:
    route: str
    status: str
    destination_folder: str
    destination_filename: str
    notification_recipient: str | None
    notification_reason: str | None


def route_confirmed_invoice(invoice: ConfirmedInvoice) -> RoutingDecision:
    _validate_common_fields(invoice)
    purchase_order_number = (invoice.purchase_order_number or "").strip()

    if purchase_order_number:
        if not (invoice.po_matching_folder or "").strip():
            raise RoutingValidationError(
                "A PO matching folder is required for a Purchase Order invoice."
            )
        if not (invoice.purchase_ledger_recipient or "").strip():
            raise RoutingValidationError(
                "A Purchase Ledger notification recipient is required for a "
                "Purchase Order invoice."
            )
        return RoutingDecision(
            route="purchase_order",
            status="Awaiting PO Matching",
            destination_folder=str(invoice.po_matching_folder),
            destination_filename=Path(invoice.original_filename).name,
            notification_recipient=str(invoice.purchase_ledger_recipient),
            notification_reason=(
                f"Invoice {invoice.invoice_id} requires matching against "
                f"Purchase Order {purchase_order_number} and goods-received information."
            ),
        )

    return RoutingDecision(
        route="nominal",
        status="Awaiting Nominal Processing",
        destination_folder=invoice.company_folder,
        destination_filename=_prefixed_filename(
            invoice.irj_number, invoice.original_filename
        ),
        notification_recipient=None,
        notification_reason=None,
    )


def _validate_common_fields(invoice: ConfirmedInvoice) -> None:
    required = {
        "invoice ID": invoice.invoice_id,
        "company": invoice.company,
        "company folder": invoice.company_folder,
        "original filename": invoice.original_filename,
        "IRJ number": invoice.irj_number,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise RoutingValidationError(
            f"Missing required confirmed invoice data: {', '.join(missing)}."
        )
    if Path(invoice.original_filename).suffix.lower() != ".pdf":
        raise RoutingValidationError("The confirmed invoice must be a PDF.")


def _prefixed_filename(irj_number: str, original_filename: str) -> str:
    safe_irj = SAFE_REFERENCE.sub("-", irj_number.strip()).strip("-._")
    if not safe_irj:
        raise RoutingValidationError("The IRJ number is not valid for a filename.")

    filename = Path(original_filename).name
    if filename.casefold().startswith(f"{safe_irj}_".casefold()):
        return filename
    return f"{safe_irj}_{filename}"

