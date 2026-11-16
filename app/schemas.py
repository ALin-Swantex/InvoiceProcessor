"""Request models and role groupings shared by the API routers."""

from __future__ import annotations

from pydantic import BaseModel

from app.activity_feed import ROLE_PURCHASE_LEDGER
from app.auth import ROLE_ADMIN, ROLE_APPROVER_1, ROLE_APPROVER_2


# ---------------------------------------------------------------------------
# Role access map — mirrors MANUAL_VS_AUTOMATED.md. Every state-changing
# endpoint and every admin configuration endpoint is restricted to the
# role(s) that should be able to perform that action; read endpoints are
# open to any signed-in user so every role can see the full pipeline (the
# frontend nav then only surfaces the sections/actions relevant to the
# signed-in role).
# ---------------------------------------------------------------------------
ROLES_PURCHASE_LEDGER_ADMIN = (ROLE_PURCHASE_LEDGER, ROLE_ADMIN)
ROLES_APPROVERS_ADMIN = (ROLE_APPROVER_1, ROLE_APPROVER_2, ROLE_ADMIN)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ConfirmationRequest(BaseModel):
    """Raw routing calculator request. Kept for direct testing of
    app.workflow.route_confirmed_invoice without any persistence or
    SharePoint side effects."""

    invoice_id: str
    company: str
    company_folder: str
    original_filename: str
    irj_number: str
    purchase_order_number: str | None = None
    po_matching_folder: str | None = None
    purchase_ledger_recipient: str | None = None


class InvoiceConfirmRequest(BaseModel):
    """Purchase Ledger's confirmation of AI-extracted (or manually entered)
    invoice fields. Triggers IRJ numbering, PO/nominal routing, SharePoint
    filing, and approver assignment."""

    company: str
    supplier: str
    supplier_invoice_number: str | None = None
    purchase_order_number: str | None = None
    invoice_date: str | None = None
    invoice_value: float | None = None
    currency: str | None = "GBP"
    override_duplicate: bool = False
    correction_reason: str | None = None


class ApprovalDecisionRequest(BaseModel):
    level: int
    decision: str
    comments: str | None = None


class PoMatchRequest(BaseModel):
    matched: bool
    notes: str | None = None
    query_category: str | None = None
    purchasing_contact: str | None = None


class FlagReviewRequest(BaseModel):
    reason: str


class ReviewDecisionRequest(BaseModel):
    accepted: bool
    reason: str | None = None


class StatementFileRequest(BaseModel):
    company: str


class PaymentRequest(BaseModel):
    payment_date: str
    supplier_account_number: str | None = None
    payment_reference: str
    payment_method: str | None = None


class ReconciliationRequest(BaseModel):
    reconciliation_date: str
    notes: str | None = None


class ResumeApprovalRequest(BaseModel):
    resolution_notes: str | None = None


class SageRegistrationRequest(BaseModel):
    irj_number: str


class PaymentRouteRequest(BaseModel):
    route: str


class RejectInvoiceRequest(BaseModel):
    reason: str


class ForeignAllocationRequest(BaseModel):
    allocation_date: str
    allocation_reference: str


class LoginRequest(BaseModel):
    username: str
    password: str


class CompanyRequest(BaseModel):
    name: str
    sharepoint_root_folder: str | None = None
    company_folder: str | None = None
    po_matching_folder: str | None = None
    aliases: list[str] | None = None
    vat_number: str | None = None
    address: str | None = None


class CompanyUpdateRequest(BaseModel):
    sharepoint_root_folder: str | None = None
    company_folder: str | None = None
    po_matching_folder: str | None = None
    aliases: list[str] | None = None
    vat_number: str | None = None
    address: str | None = None


class SupplierRequest(BaseModel):
    name: str
    aliases: list[str] | None = None
    default_company: str | None = None
    contact_email: str | None = None
    invoice_number_pattern: str | None = None


class SupplierUpdateRequest(BaseModel):
    aliases: list[str] | None = None
    default_company: str | None = None
    contact_email: str | None = None
    invoice_number_pattern: str | None = None


class ApprovalMatrixRequest(BaseModel):
    company: str
    supplier: str
    approver1_name: str
    approver1_email: str
    approver2_name: str | None = None
    approver2_email: str | None = None


class ApprovalMatrixUpdateRequest(BaseModel):
    company: str | None = None
    supplier: str | None = None
    approver1_name: str | None = None
    approver1_email: str | None = None
    approver2_name: str | None = None
    approver2_email: str | None = None


class SupplierTermsUpdateRequest(BaseModel):
    company: str
    supplier: str
    supplier_account_number: str | None = None
    default_payment_method: str | None = None
    payment_terms_notice: str | None = None
    bank_account: str | None = None


class ThresholdUpdateRequest(BaseModel):
    threshold: float


class IrjConfigurationUpdateRequest(BaseModel):
    mode: str
    current_irj: str | None = None
