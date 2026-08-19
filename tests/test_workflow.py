import pytest

from app.workflow import (
    ConfirmedInvoice,
    RoutingValidationError,
    route_confirmed_invoice,
)


def confirmed_invoice(
    *,
    original_filename: str = "supplier-invoice.pdf",
    irj_number: str = "IRJ 001245",
    purchase_order_number: str | None = None,
    po_matching_folder: str | None = None,
    purchase_ledger_recipient: str | None = None,
) -> ConfirmedInvoice:
    return ConfirmedInvoice(
        invoice_id="invoice-123",
        company="Example Company",
        company_folder="/Companies/Example/Invoices",
        original_filename=original_filename,
        irj_number=irj_number,
        purchase_order_number=purchase_order_number,
        po_matching_folder=po_matching_folder,
        purchase_ledger_recipient=purchase_ledger_recipient,
    )


def test_po_invoice_routes_to_matching_and_plans_notification() -> None:
    decision = route_confirmed_invoice(
        confirmed_invoice(
            purchase_order_number="PO-7788",
            po_matching_folder="/Companies/Example/PO Matching",
            purchase_ledger_recipient="purchase-ledger@example.test",
        )
    )

    assert decision.route == "purchase_order"
    assert decision.status == "Awaiting PO Matching"
    assert decision.destination_folder == "/Companies/Example/PO Matching"
    assert decision.destination_filename == "supplier-invoice.pdf"
    assert decision.notification_recipient == "purchase-ledger@example.test"
    assert "PO-7788" in str(decision.notification_reason)


def test_non_po_invoice_routes_to_company_and_prefixes_irj() -> None:
    decision = route_confirmed_invoice(confirmed_invoice())

    assert decision.route == "nominal"
    assert decision.status == "Awaiting Nominal Processing"
    assert decision.destination_folder == "/Companies/Example/Invoices"
    assert decision.destination_filename == "IRJ-001245_supplier-invoice.pdf"
    assert decision.notification_recipient is None


def test_irj_prefix_is_idempotent() -> None:
    decision = route_confirmed_invoice(
        confirmed_invoice(
            irj_number="IRJ-001245",
            original_filename="IRJ-001245_supplier-invoice.pdf",
        )
    )

    assert decision.destination_filename == "IRJ-001245_supplier-invoice.pdf"


def test_po_invoice_requires_notification_configuration() -> None:
    with pytest.raises(RoutingValidationError, match="notification recipient"):
        route_confirmed_invoice(
            confirmed_invoice(
                purchase_order_number="PO-7788",
                po_matching_folder="/Companies/Example/PO Matching",
            )
        )
