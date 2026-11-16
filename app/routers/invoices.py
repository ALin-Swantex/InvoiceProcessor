"""Invoice listing, search, manual upload and workflow actions."""

from __future__ import annotations

import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.activity_feed import ROLE_PURCHASE_LEDGER
from app.auth import ROLE_APPROVER_1, ROLE_APPROVER_2, User, get_current_user, require_role
from app.deps import (
    get_lifecycle,
    invoice_for_user,
    invoice_is_visible_to_user,
    invoices_for_user,
    require_assigned_approver,
)
from app.invoice_lifecycle import InvoiceExtractionUnavailableError, InvoiceLifecycleError
from app.pdf_validation import InvalidPdfError, validate_pdf
from app.schemas import (
    ROLES_APPROVERS_ADMIN,
    ROLES_PURCHASE_LEDGER_ADMIN,
    ApprovalDecisionRequest,
    ConfirmationRequest,
    FlagReviewRequest,
    ForeignAllocationRequest,
    InvoiceConfirmRequest,
    PaymentRequest,
    PaymentRouteRequest,
    PoMatchRequest,
    ReconciliationRequest,
    RejectInvoiceRequest,
    ResumeApprovalRequest,
    ReviewDecisionRequest,
    SageRegistrationRequest,
    StatementFileRequest,
)
from app.sharepoint import SharePointError
from app.sharepoint_intake import SharePointIncomingMonitor
from app.workflow import ConfirmedInvoice, RoutingValidationError, route_confirmed_invoice

router = APIRouter()


@router.get("/api/invoices")
def list_invoices(
    http_request: Request,
    limit: int = Query(100, ge=1, le=500), user: User = Depends(get_current_user)
) -> list[dict[str, object]]:
    app = http_request.app
    invoices = invoices_for_user(app, user, limit)
    return [asdict(invoice) for invoice in invoices]


@router.post("/api/invoices/manual-upload")
def manual_upload_invoice(
    http_request: Request,
    file: UploadFile = File(...),
    sender_name: str | None = Form(None),
    sender_address: str | None = Form(None),
    subject: str | None = Form(None),
    received_at: str | None = Form(None),
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    """Manually add an invoice PDF into the same Incoming Invoices
    workflow used for Outlook-sourced invoices, per SOFTWARE_SPEC.md
    section 3: "There should also be a way of manually adding an
    invoice to the Incoming Invoices folder so that invoices received
    outside of the normal email process can still enter the same
    workflow."

    In the connected deployment, the PDF is uploaded to SharePoint
    Incoming first and registered from its DriveItem identity. A local
    cache is retained only for AI extraction and PDF preview.

    This is a plain (sync) handler so FastAPI runs it in its threadpool:
    the SharePoint upload, AI extraction and store writes below are all
    blocking and must not stall the event loop.
    """
    import app.main as main_module
    app = http_request.app
    if file.content_type not in {"application/pdf", "application/octet-stream"} and not (
        file.filename or ""
    ).lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files may be uploaded.")

    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file was empty.")
    try:
        validate_pdf(content)
    except InvalidPdfError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    unique_id = uuid.uuid4().hex
    original_filename = file.filename or f"manual-invoice-{unique_id}.pdf"

    message = {
        "internetMessageId": None,
        "subject": subject or "(Manually added invoice)",
        "receivedDateTime": received_at
        or datetime.now(timezone.utc).isoformat(),
        "from": {
            "emailAddress": {
                "name": sender_name or "Manually added",
                "address": sender_address or "",
            }
        },
    }

    lifecycle = get_lifecycle(app)
    if lifecycle.sharepoint_client is not None:
        monitor = SharePointIncomingMonitor(
            lifecycle.sharepoint_client,
            app.state.invoice_store,
            cache_directory=Path(
                os.environ.get(
                    "SHAREPOINT_INVOICE_CACHE_DIR",
                    "runtime_data/sharepoint_invoice_cache",
                )
            ),
            extraction_runner=(
                lifecycle.run_extraction
                if main_module.ai_extraction_configured()
                else lifecycle.run_document_classification
            ),
            activity_feed=app.state.activity_feed,
        )
        try:
            item = lifecycle.sharepoint_client.upload_to_incoming(
                original_filename,
                content,
            )
        except (SharePointError, InvoiceExtractionUnavailableError) as error:
            # Nothing was uploaded, so there is no record to fall back to.
            raise HTTPException(
                status_code=503,
                detail=f"SharePoint Incoming upload failed: {error}",
            ) from error
        try:
            record = monitor.ingest_item(
                item,
                source_message=message,
                content=content,
                event_type="manual_intake",
            )
        except InvoiceExtractionUnavailableError:
            # The item was registered before extraction failed; return it
            # so it stays visible for manual review.
            item_id = str(item.get("id", ""))
            record = app.state.invoice_store.get_by_sharepoint_item_id(item_id)
        except SharePointError as error:
            raise HTTPException(
                status_code=503,
                detail=f"SharePoint Incoming upload failed: {error}",
            ) from error
        if record is None:
            raise HTTPException(
                status_code=409,
                detail="The SharePoint invoice is already registered.",
            )
    else:
        upload_dir = Path(os.environ.get("MANUAL_UPLOAD_DIR", "manual_uploads"))
        upload_dir.mkdir(parents=True, exist_ok=True)
        stored_path = upload_dir / f"{unique_id}-{original_filename}"
        stored_path.write_bytes(content)
        message["id"] = f"manual-{unique_id}"
        attachment = {
            "id": f"manual-attachment-{unique_id}",
            "name": original_filename,
            "size": len(content),
        }
        record = app.state.invoice_store.add_from_outlook(
            message=message, attachment=attachment, stored_path=stored_path
        )
        app.state.activity_feed.add_event(
            event_type="manual_intake",
            target_role=ROLE_PURCHASE_LEDGER,
            message=(
                f"'{original_filename}' was manually added in offline mode "
                "and is awaiting AI extraction."
            ),
            invoice_id=record.id,
        )
        if main_module.ai_extraction_configured():
            try:
                record = lifecycle.run_extraction(record.id)
            except InvoiceExtractionUnavailableError:
                record = app.state.invoice_store.get(record.id) or record
        else:
            record = lifecycle.run_document_classification(record.id)
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/register-sage")
def register_invoice_in_sage(
    http_request: Request,
    invoice_id: int,
    request: SageRegistrationRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).register_in_sage(
            invoice_id,
            irj_number=request.irj_number,
            recorded_by=user.display_name,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/payment-route")
def choose_payment_route(
    http_request: Request,
    invoice_id: int,
    request: PaymentRouteRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).route_for_payment(
            invoice_id,
            route=request.route,
            recorded_by=user.display_name,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/retry-payment-route")
def retry_invoice_payment_route(
    http_request: Request,
    invoice_id: int,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).auto_route_for_payment(invoice_id)
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/reject")
def reject_invoice(
    http_request: Request,
    invoice_id: int,
    request: RejectInvoiceRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).reject_invoice(
            invoice_id, reason=request.reason, recorded_by=user.display_name
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/cancel-duplicate")
def cancel_duplicate_invoice(
    http_request: Request,
    invoice_id: int,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).cancel_confirmed_duplicate(
            invoice_id, recorded_by=user.display_name
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.get("/api/invoices/{invoice_id}")
def get_invoice(http_request: Request, invoice_id: int, user: User = Depends(get_current_user)) -> dict[str, object]:
    app = http_request.app
    invoice = invoice_for_user(app, invoice_id, user)
    return asdict(invoice)


@router.delete("/api/invoices/{invoice_id}")
def delete_invoice(
    http_request: Request,
    invoice_id: int,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        get_lifecycle(app).delete_invoice(
            invoice_id,
            recorded_by=user.display_name,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"id": invoice_id, "deleted": True}


@router.get("/api/invoice-search")
def search_invoice_by_irj(
    http_request: Request,
    irj_number: str = Query(..., min_length=1, max_length=32),
    user: User = Depends(get_current_user),
) -> dict[str, object]:
    app = http_request.app
    normalized = irj_number.strip()
    if len(normalized) != 6 or not normalized.isdigit():
        raise HTTPException(
            status_code=422,
            detail="An IRJ number must contain exactly six digits.",
        )
    invoice = app.state.invoice_store.get_by_irj_number(normalized)
    if invoice is None or not invoice_is_visible_to_user(invoice, user):
        raise HTTPException(
            status_code=404,
            detail=f"No invoice was found for IRJ number '{normalized}'.",
        )
    result = asdict(invoice)
    result["audit_trail"] = [
        asdict(event)
        for event in app.state.activity_feed.list_for_invoice(invoice.id)
    ]
    return result


@router.get("/api/invoice-search/filter")
def filter_invoices(
    http_request: Request,
    irj_number: str | None = Query(None, max_length=32),
    company: str | None = Query(None, max_length=255),
    supplier: str | None = Query(None, max_length=255),
    user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    app = http_request.app
    normalized_irj = irj_number.strip() if irj_number else None
    normalized_company = company.strip().casefold() if company else None
    normalized_supplier = supplier.strip().casefold() if supplier else None
    if normalized_irj and (
        len(normalized_irj) != 6 or not normalized_irj.isdigit()
    ):
        raise HTTPException(
            status_code=422,
            detail="An IRJ number must contain exactly six digits.",
        )
    if not normalized_irj and not normalized_company and not normalized_supplier:
        raise HTTPException(
            status_code=422,
            detail="Provide an IRJ number, company, or supplier.",
        )
    matches = []
    candidates = (
        invoices_for_user(app, user, 500)
        if user.role in {ROLE_APPROVER_1, ROLE_APPROVER_2}
        else app.state.invoice_store.list_all()
    )
    for invoice in candidates:
        if normalized_irj and invoice.irj_number != normalized_irj:
            continue
        if normalized_company and (
            not invoice.company
            or invoice.company.strip().casefold() != normalized_company
        ):
            continue
        if normalized_supplier and (
            not invoice.supplier
            or invoice.supplier.strip().casefold() != normalized_supplier
        ):
            continue
        result = asdict(invoice)
        if normalized_irj:
            result["audit_trail"] = [
                asdict(event)
                for event in app.state.activity_feed.list_for_invoice(invoice.id)
            ]
        matches.append(result)
    return matches


@router.get("/api/invoices/{invoice_id}/pdf")
def get_invoice_pdf(
    http_request: Request,
    invoice_id: int, user: User = Depends(get_current_user)
) -> FileResponse:
    app = http_request.app
    invoice = invoice_for_user(app, invoice_id, user)
    pdf_path = Path(invoice.stored_path)
    if not pdf_path.is_file():
        raise HTTPException(status_code=404, detail="Invoice PDF was not found.")
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=invoice.original_filename,
        content_disposition_type="inline",
    )


@router.post("/api/invoices/{invoice_id}/extract")
def extract_invoice(
    http_request: Request,
    invoice_id: int, user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN))
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).run_extraction(invoice_id)
    except InvoiceExtractionUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/route-foreign-payment")
def route_foreign_payment(
    http_request: Request,
    invoice_id: int,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).route_as_foreign_payment(
            invoice_id, recorded_by=user.display_name
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/allocate-foreign-payment")
def allocate_foreign_payment(
    http_request: Request,
    invoice_id: int,
    request: ForeignAllocationRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).mark_foreign_allocated(
            invoice_id,
            allocation_date=request.allocation_date,
            allocation_reference=request.allocation_reference,
            recorded_by=user.display_name,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/revert-foreign-payment")
def revert_foreign_payment(
    http_request: Request,
    invoice_id: int,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).revert_foreign_payment_route(
            invoice_id, recorded_by=user.display_name
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/confirm")
def confirm_and_route_invoice(
    http_request: Request,
    invoice_id: int,
    request: InvoiceConfirmRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).confirm_and_route(
            invoice_id,
            company=request.company,
            supplier=request.supplier,
            supplier_invoice_number=request.supplier_invoice_number,
            purchase_order_number=request.purchase_order_number,
            invoice_date=request.invoice_date,
            invoice_value=request.invoice_value,
            currency=request.currency,
            override_duplicate=request.override_duplicate,
            recorded_by=user.display_name,
            correction_reason=request.correction_reason,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/po-match")
def po_match_invoice(
    http_request: Request,
    invoice_id: int,
    request: PoMatchRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).record_po_match(
            invoice_id,
            matched=request.matched,
            notes=request.notes,
            query_category=request.query_category,
            purchasing_contact=request.purchasing_contact,
            recorded_by=user.display_name,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/approve")
def approve_invoice(
    http_request: Request,
    invoice_id: int,
    request: ApprovalDecisionRequest,
    user: User = Depends(require_role(*ROLES_APPROVERS_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    # Approver 1 may only decide level-1 approvals, Approver 2 only
    # level-2 -- per MANUAL_VS_AUTOMATED.md each approver is a distinct
    # human decision step and must not be able to act on the other
    # level. Admin may act at either level (support/break-glass).
    if user.role == ROLE_APPROVER_1 and request.level != 1:
        raise HTTPException(
            status_code=403, detail="Approver 1 can only decide level-1 approvals."
        )
    if user.role == ROLE_APPROVER_2 and request.level != 2:
        raise HTTPException(
            status_code=403, detail="Approver 2 can only decide level-2 approvals."
        )
    require_assigned_approver(app, invoice_id, user, request.level)
    try:
        record = get_lifecycle(app).decide_approval(
            invoice_id,
            level=request.level,
            decision=request.decision,
            comments=request.comments,
            recorded_by=user.display_name,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/resume-approval")
def resume_approval_invoice(
    http_request: Request,
    invoice_id: int,
    request: ResumeApprovalRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).resume_approval(
            invoice_id,
            resolution_notes=request.resolution_notes,
            recorded_by=user.display_name,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/flag-review")
def flag_invoice_for_review(
    http_request: Request,
    invoice_id: int,
    request: FlagReviewRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).flag_for_review(invoice_id, reason=request.reason)
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/review-decision")
def decide_flagged_invoice(
    http_request: Request,
    invoice_id: int,
    request: ReviewDecisionRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).resolve_flagged_review(
            invoice_id,
            accepted=request.accepted,
            reason=request.reason,
            recorded_by=user.display_name,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/reject-flagged")
def reject_flagged_invoice(
    http_request: Request,
    invoice_id: int,
    request: RejectInvoiceRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).reject_flagged_invoice(
            invoice_id,
            reason=request.reason,
            recorded_by=user.display_name,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/file-statement")
def file_supplier_statement(
    http_request: Request,
    invoice_id: int,
    request: StatementFileRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).file_statement(
            invoice_id,
            company=request.company,
            recorded_by=user.display_name,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/mark-as-invoice")
def mark_document_as_invoice(
    http_request: Request,
    invoice_id: int,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).mark_as_invoice(
            invoice_id,
            recorded_by=user.display_name,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/pay")
def pay_invoice(
    http_request: Request,
    invoice_id: int,
    request: PaymentRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).mark_paid(
            invoice_id,
            payment_date=request.payment_date,
            supplier_account_number=request.supplier_account_number,
            payment_reference=request.payment_reference,
            payment_method=request.payment_method,
            recorded_by=user.display_name,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/invoices/{invoice_id}/reconcile")
def reconcile_invoice(
    http_request: Request,
    invoice_id: int,
    request: ReconciliationRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        record = get_lifecycle(app).mark_reconciled(
            invoice_id,
            reconciliation_date=request.reconciliation_date,
            notes=request.notes,
            recorded_by=user.display_name,
        )
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


@router.post("/api/workflow/confirm")
def confirm_invoice(
    request: ConfirmationRequest,
    user: User = Depends(require_role(*ROLES_PURCHASE_LEDGER_ADMIN)),
) -> dict[str, str | None]:
    try:
        decision = route_confirmed_invoice(ConfirmedInvoice(**request.model_dump()))
    except RoutingValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "route": decision.route,
        "status": decision.status,
        "destination_folder": decision.destination_folder,
        "destination_filename": decision.destination_filename,
        "notification_recipient": decision.notification_recipient,
        "notification_reason": decision.notification_reason,
    }
