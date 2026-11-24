"""Stage 4: payment routing, payment, foreign allocation and bank reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone

from app.invoices import InvoiceRecord
from app.statuses import Status
from app.lifecycle.common import InvoiceLifecycleError
from app.lifecycle.base import LifecycleBase


class PaymentStage(LifecycleBase):
    def route_for_payment(
        self, invoice_id: int, *, route: str, recorded_by: str
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status not in (Status.APPROVED, Status.PAYMENT_ROUTING_ISSUE):
            raise InvoiceLifecycleError(
                f"Only approved invoices can be routed for payment "
                f"(status: {invoice.status})."
            )
        routes = {
            "bacs": (Status.APPROVED_BACS, "BACS"),
            "bankline": (Status.APPROVED_BANKLINE, "Bankline"),
            "foreign_poa": (Status.APPROVED_FOREIGN_POA, "Foreign POA"),
        }
        if route not in routes:
            raise InvoiceLifecycleError(
                "Payment route must be BACS, Bankline, or Foreign POA."
            )
        status, payment_method = routes[route]
        record = self._transition(
            invoice,
            status,
            payment_method=payment_method,
            is_foreign_payment=1 if route == "foreign_poa" else 0,
            payment_route_decided_at=datetime.now(timezone.utc).isoformat(),
            payment_route_decided_by=recorded_by,
            payment_date=None,
            payment_reference=None,
            paid_by=None,
            reconciliation_date=None,
            reconciliation_notes=None,
            reconciled_by=None,
            foreign_allocation_date=None,
            foreign_allocation_reference=None,
            foreign_allocated_by=None,
            hold_reason=None,
        )
        self._log_purchase_ledger(
            event_type="payment_route_selected",
            message=(
                f"Invoice {invoice.irj_number} routed to {payment_method} "
                f"by {recorded_by}."
            ),
            invoice_id=invoice_id,
        )
        return record

    @staticmethod
    def _normalize_payment_route(value: str | None) -> str | None:
        normalized = (value or "").strip().casefold().replace("_", " ").replace("-", " ")
        normalized = " ".join(normalized.split())
        aliases = {
            "bacs": "bacs",
            "bankline": "bankline",
            "foreign poa": "foreign_poa",
            "foreign": "foreign_poa",
        }
        return aliases.get(normalized)

    def auto_route_for_payment(self, invoice_id: int) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status not in (Status.APPROVED, Status.PAYMENT_ROUTING_ISSUE):
            raise InvoiceLifecycleError(
                f"Invoice {invoice_id} is not ready for payment routing "
                f"(status: {invoice.status})."
            )
        reason: str | None = None
        route: str | None = None
        if self.supplier_terms_store is None:
            reason = "Supplier payment settings are unavailable."
        else:
            profiles = self.supplier_terms_store.list_for_supplier(
                str(invoice.company or ""), str(invoice.supplier or "")
            )
            configured = {
                self._normalize_payment_route(profile.default_payment_method)
                for profile in profiles
                if profile.default_payment_method
            }
            invalid = [
                profile.default_payment_method
                for profile in profiles
                if profile.default_payment_method
                and self._normalize_payment_route(profile.default_payment_method) is None
            ]
            if not profiles or not configured:
                reason = (
                    "No supported default payment method is configured for this "
                    "company and supplier."
                )
            elif invalid:
                reason = f"Unsupported supplier payment method: {invalid[0]}."
            elif len(configured) > 1:
                reason = "Supplier account profiles contain conflicting payment methods."
            else:
                route = next(iter(configured))
        if route is not None:
            return self.route_for_payment(
                invoice_id,
                route=route,
                recorded_by="Automatic supplier payment routing",
            )
        record = self._transition(
            invoice,
            Status.PAYMENT_ROUTING_ISSUE,
            move_pdf=False,
            payment_method=None,
            hold_reason=reason,
            payment_route_decided_at=None,
            payment_route_decided_by=None,
        )
        self._log_purchase_ledger(
            event_type="payment_routing_on_hold",
            message=(
                f"Invoice {invoice.irj_number or invoice.original_filename} is on hold: "
                f"{reason}"
            ),
            invoice_id=invoice_id,
        )
        return record

    def mark_paid(
        self,
        invoice_id: int,
        *,
        payment_date: str,
        payment_reference: str | None,
        supplier_account_number: str | None = None,
        payment_method: str | None = None,
        recorded_by: str | None = None,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        payable_statuses = {
            Status.APPROVED_BACS: "BACS",
            Status.APPROVED_BANKLINE: "Bankline",
            Status.APPROVED_FOREIGN_POA: "Foreign POA",
            Status.FOREIGN_AWAITING_ALLOCATION: "Foreign POA",
        }
        if invoice.status not in payable_statuses:
            raise InvoiceLifecycleError(
                f"Only an invoice in a payment section can be marked as paid "
                f"(status: {invoice.status})."
            )
        if not payment_reference or not payment_reference.strip():
            raise InvoiceLifecycleError("A payment reference is required.")
        if not payment_date.strip():
            raise InvoiceLifecycleError("A payment date is required.")
        # Record who marked the invoice as paid.
        record = self._transition(
            invoice,
            Status.PAID,
            payment_date=payment_date,
            supplier_account_number=supplier_account_number,
            payment_reference=payment_reference,
            payment_method=payable_statuses[invoice.status],
            paid_by=recorded_by,
        )
        self._log_purchase_ledger(
            event_type="paid",
            message=f"Invoice {invoice.irj_number} paid; awaiting bank reconciliation.",
            invoice_id=invoice_id,
        )
        return record

    def route_as_foreign_payment(
        self, invoice_id: int, *, recorded_by: str
    ) -> InvoiceRecord:
        return self.route_for_payment(
            invoice_id, route="foreign_poa", recorded_by=recorded_by
        )

    def mark_foreign_allocated(
        self,
        invoice_id: int,
        *,
        allocation_date: str,
        allocation_reference: str,
        recorded_by: str,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status not in {
            Status.FOREIGN_AWAITING_ALLOCATION,
            Status.APPROVED_FOREIGN_POA,
        }:
            raise InvoiceLifecycleError(
                f"Only foreign payments awaiting allocation can be allocated "
                f"(status: {invoice.status})."
            )
        if not allocation_reference.strip():
            raise InvoiceLifecycleError("An allocation reference is required.")
        if not allocation_date.strip():
            raise InvoiceLifecycleError("An allocation date is required.")
        record = self._transition(
            invoice,
            Status.PAID,
            payment_date=allocation_date,
            payment_reference=allocation_reference.strip(),
            payment_method="Foreign POA",
            paid_by=recorded_by,
            foreign_allocation_date=allocation_date,
            foreign_allocation_reference=allocation_reference.strip(),
            foreign_allocated_by=recorded_by,
        )
        self._log_purchase_ledger(
            event_type="foreign_payment_allocated",
            message=(
                f"Foreign payment for invoice {invoice.irj_number} allocated and "
                "is awaiting bank reconciliation."
            ),
            invoice_id=invoice_id,
        )
        return record

    def revert_foreign_payment_route(
        self, invoice_id: int, *, recorded_by: str
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status not in {
            Status.FOREIGN_AWAITING_ALLOCATION,
            Status.APPROVED_FOREIGN_POA,
        }:
            raise InvoiceLifecycleError(
                f"Only a foreign payment awaiting allocation can be returned to "
                f"domestic payment (status: {invoice.status})."
            )
        record = self._transition(
            invoice,
            Status.APPROVED,
            payment_method=None,
            is_foreign_payment=None,
            payment_route_decided_at=None,
            payment_route_decided_by=None,
            foreign_allocation_date=None,
            foreign_allocation_reference=None,
            foreign_allocated_by=None,
        )
        self._log_purchase_ledger(
            event_type="foreign_payment_reverted",
            message=(
                f"Invoice {invoice.irj_number} returned to domestic payment by "
                f"{recorded_by}."
            ),
            invoice_id=invoice_id,
        )
        return record

    def mark_reconciled(
        self,
        invoice_id: int,
        *,
        reconciliation_date: str,
        notes: str | None,
        recorded_by: str | None = None,
    ) -> InvoiceRecord:
        invoice = self._require_invoice(invoice_id)
        if invoice.status != Status.PAID:
            raise InvoiceLifecycleError(
                f"Only invoices awaiting bank reconciliation can be reconciled "
                f"(status: {invoice.status})."
            )
        # Record who completed the reconciliation, where practical.
        # reconciliation_date is the date Purchase Ledger says the payment
        # appears on the bank statement; reconciled_at is the system
        # timestamp of when the invoice was actually marked reconciled here.
        record = self._transition(
            invoice,
            Status.RECONCILED,
            reconciliation_date=reconciliation_date,
            reconciliation_notes=notes,
            reconciled_by=recorded_by,
            reconciled_at=datetime.now(timezone.utc).isoformat(),
        )
        self._log_purchase_ledger(
            event_type="reconciled",
            message=f"Invoice {invoice.irj_number} reconciled and filed.",
            invoice_id=invoice_id,
        )
        return record
