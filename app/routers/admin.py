"""Admin configuration: companies, suppliers, approval matrix, supplier
terms, metrics, operations, bulk import, AI threshold and IRJ configuration.
All admin-only (see SOFTWARE_SPEC.md: config must be maintainable by an
authorised staff member without touching code)."""

from __future__ import annotations

import io
import os
from dataclasses import asdict

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile

from app.approval_matrix import ApprovalMatrixEntry
from app.auth import ROLE_ADMIN, User, require_role
from app.bulk_import import find_existing_supplier_imports, import_supplier_workbook
from app.company_folders import (
    FLAGGED_INVOICES_FOLDER,
    INCOMING_INVOICES_FOLDER,
    REJECTED_INVOICES_FOLDER,
    CompanyFolderStructure,
    discover_company_folder_structures,
)
from app.deps import (
    company_response,
    irj_configuration,
    require_sharepoint_client,
    sync_sharepoint_companies,
)
from app.metrics import build_metrics
from app.schemas import (
    ApprovalMatrixRequest,
    ApprovalMatrixUpdateRequest,
    CompanyRequest,
    CompanyUpdateRequest,
    IrjConfigurationUpdateRequest,
    SupplierRequest,
    SupplierTermsUpdateRequest,
    SupplierUpdateRequest,
    ThresholdUpdateRequest,
)
from app.sharepoint import SharePointError

router = APIRouter()


@router.get("/api/admin/companies")
def admin_list_companies(
    http_request: Request,
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> list[dict[str, object]]:
    app = http_request.app
    sync_sharepoint_companies(app)
    return [company_response(profile) for profile in app.state.companies_store.list()]


def _sharepoint_folder_paths(app: FastAPI) -> list[str]:
    client = require_sharepoint_client(app)
    try:
        return client.list_folder_paths()
    except SharePointError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/api/admin/sharepoint/folders")
def admin_list_sharepoint_folders(
    http_request: Request,
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    folders = _sharepoint_folder_paths(app)
    app.state.sharepoint_folder_paths = frozenset(folders)
    structures = discover_company_folder_structures(folders)
    app.state.sharepoint_company_structures = {
        structure.root: structure for structure in structures
    }
    return {
        "folders": folders,
        "company_roots": [structure.as_dict() for structure in structures],
        "shared_folders": {
            "incoming": INCOMING_INVOICES_FOLDER,
            "flagged": FLAGGED_INVOICES_FOLDER,
            "rejected": REJECTED_INVOICES_FOLDER,
        },
    }


def _available_company_folder_structures(app: FastAPI) -> dict[str, CompanyFolderStructure]:
    return {
        structure.root: structure
        for structure in discover_company_folder_structures(
            _sharepoint_folder_paths(app)
        )
    }


@router.post("/api/admin/companies")
def admin_create_company(
    http_request: Request,
    request: CompanyRequest, user: User = Depends(require_role(ROLE_ADMIN))
) -> dict[str, object]:
    app = http_request.app
    request_fields = request.model_dump()
    root = request_fields.pop("sharepoint_root_folder")
    structure: CompanyFolderStructure | None = None
    if root:
        structure = _available_company_folder_structures(app).get(root)
        if structure is None:
            raise HTTPException(
                status_code=400,
                detail="Select a complete company folder structure from SharePoint.",
            )
        request_fields["sharepoint_root_folder"] = structure.root
        request_fields["company_folder"] = structure.nominal_invoices
        request_fields["po_matching_folder"] = structure.po_match
    elif not request.company_folder or not request.po_matching_folder:
        raise HTTPException(
            status_code=400,
            detail="Select a SharePoint company folder.",
        )
    try:
        profile = app.state.companies_store.create(**request_fields)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return company_response(profile)


@router.put("/api/admin/companies/{name}")
def admin_update_company(
    http_request: Request,
    name: str,
    request: CompanyUpdateRequest,
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    fields = request.model_dump(exclude_unset=True)
    root = fields.get("sharepoint_root_folder")
    if isinstance(root, str):
        structure = _available_company_folder_structures(app).get(root)
        if structure is None:
            raise HTTPException(
                status_code=400,
                detail="Select a complete company folder structure from SharePoint.",
            )
        fields.update(
            sharepoint_root_folder=structure.root,
            company_folder=structure.nominal_invoices,
            po_matching_folder=structure.po_match,
        )
    try:
        profile = app.state.companies_store.update(name, **fields)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return company_response(profile)


@router.delete("/api/admin/companies/{name}")
def admin_delete_company(
    http_request: Request,
    name: str, user: User = Depends(require_role(ROLE_ADMIN))
) -> dict[str, str]:
    app = http_request.app
    try:
        app.state.companies_store.delete(name)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    app.state.approval_matrix_store.delete_by_company(name)
    app.state.supplier_terms_store.delete_by_company(name)
    return {"status": "deleted"}


@router.get("/api/admin/suppliers")
def admin_list_suppliers(
    http_request: Request,
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> list[dict[str, object]]:
    app = http_request.app
    return [asdict(profile) for profile in app.state.suppliers_store.list()]


@router.post("/api/admin/suppliers")
def admin_create_supplier(
    http_request: Request,
    request: SupplierRequest, user: User = Depends(require_role(ROLE_ADMIN))
) -> dict[str, object]:
    app = http_request.app
    try:
        profile = app.state.suppliers_store.create(**request.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(profile)


@router.put("/api/admin/suppliers/{name}")
def admin_update_supplier(
    http_request: Request,
    name: str,
    request: SupplierUpdateRequest,
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    fields = request.model_dump(exclude_unset=True)
    try:
        profile = app.state.suppliers_store.update(name, **fields)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(profile)


@router.delete("/api/admin/suppliers/{name}")
def admin_delete_supplier(
    http_request: Request,
    name: str, user: User = Depends(require_role(ROLE_ADMIN))
) -> dict[str, str]:
    app = http_request.app
    try:
        app.state.suppliers_store.delete(name)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    app.state.approval_matrix_store.delete_by_supplier(name)
    app.state.supplier_terms_store.delete_by_supplier(name)
    return {"status": "deleted"}


def _matrix_entry_response(entry: ApprovalMatrixEntry) -> dict[str, object]:
    return {
        "id": entry.id,
        "company": entry.company,
        "supplier": entry.supplier,
        "approver1_name": entry.approver1.name,
        "approver1_email": entry.approver1.email,
        "approver2_name": entry.approver2.name if entry.approver2 else None,
        "approver2_email": entry.approver2.email if entry.approver2 else None,
    }


@router.get("/api/admin/approval-matrix")
def admin_list_approval_matrix(
    http_request: Request,
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> list[dict[str, object]]:
    app = http_request.app
    return [
        _matrix_entry_response(entry)
        for entry in app.state.approval_matrix_store.list()
    ]


@router.post("/api/admin/approval-matrix")
def admin_create_approval_matrix_entry(
    http_request: Request,
    request: ApprovalMatrixRequest, user: User = Depends(require_role(ROLE_ADMIN))
) -> dict[str, object]:
    app = http_request.app
    try:
        fields = request.model_dump()
        existing = app.state.approval_matrix_store.find_exact(
            request.company, request.supplier
        )
        if existing is None:
            entry = app.state.approval_matrix_store.create(**fields)
        else:
            entry = app.state.approval_matrix_store.update(existing.id, **fields)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _matrix_entry_response(entry)


@router.put("/api/admin/approval-matrix/{entry_id}")
def admin_update_approval_matrix_entry(
    http_request: Request,
    entry_id: int,
    request: ApprovalMatrixUpdateRequest,
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    fields = request.model_dump(exclude_unset=True)
    try:
        entry = app.state.approval_matrix_store.update(entry_id, **fields)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _matrix_entry_response(entry)


@router.delete("/api/admin/approval-matrix/{entry_id}")
def admin_delete_approval_matrix_entry(
    http_request: Request,
    entry_id: int, user: User = Depends(require_role(ROLE_ADMIN))
) -> dict[str, str]:
    app = http_request.app
    try:
        app.state.approval_matrix_store.delete(entry_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"status": "deleted"}


@router.get("/api/admin/supplier-terms")
def admin_list_supplier_terms(
    http_request: Request,
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> list[dict[str, object]]:
    app = http_request.app
    return [asdict(terms) for terms in app.state.supplier_terms_store.list()]


@router.get("/api/admin/metrics")
def admin_metrics(
    http_request: Request,
    granularity: str = Query("month", pattern="^(day|week|month)$"),
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    del user
    return build_metrics(
        app.state.invoice_store.list_all(),
        app.state.supplier_terms_store.list(),
        granularity=granularity,
    )


@router.get("/api/admin/operations")
def admin_operations(
    http_request: Request,
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    del user
    worker = (
        app.state.worker_monitor.status()
        if app.state.worker_monitor is not None
        else None
    )
    queue = {
        status: [asdict(item) for item in app.state.notification_store.list(
            status=status, limit=50
        )]
        for status in ("pending", "processing", "failed")
    }
    stale_after = int(os.environ.get("WORKER_STALE_AFTER_SECONDS", "120"))
    alerts: list[str] = []
    if worker is None:
        alerts.append("The Outlook worker has not reported a heartbeat.")
    elif int(worker["age_seconds"]) > stale_after:
        alerts.append(
            f"The Outlook worker heartbeat is {worker['age_seconds']} seconds old."
        )
    if queue["failed"]:
        alerts.append(f"{len(queue['failed'])} Outlook queue item(s) have failed.")
    if queue["processing"]:
        alerts.append(
            f"{len(queue['processing'])} Outlook queue item(s) are processing."
        )
    return {"worker": worker, "queue": queue, "alerts": alerts}


@router.post("/api/admin/outlook-queue/{notification_id}/retry")
def admin_retry_outlook_notification(
    http_request: Request,
    notification_id: int,
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    del user
    try:
        app.state.notification_store.retry_failed(notification_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"id": notification_id, "status": "pending"}


@router.put("/api/admin/supplier-terms/{terms_id}")
def admin_update_supplier_terms(
    http_request: Request,
    terms_id: int,
    request: SupplierTermsUpdateRequest,
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    try:
        terms = app.state.supplier_terms_store.update(
            terms_id, **request.model_dump()
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(terms)


@router.post("/api/admin/import/supplier-master-data")
async def admin_import_supplier_master_data(
    http_request: Request,
    file: UploadFile = File(...),
    company: str = Form(...),
    replace_existing: bool = Form(False),
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, object]:
    """Bulk-import supplier companies, approval matrix entries, and
    supplier payment terms from an uploaded .xlsx workbook, scoped to
    the company selected by the admin. A Company column in the workbook
    is ignored when this selection is provided. Expected columns (header
    names are flexible, see
    app/bulk_import.py): Trading Partner Name, Supplier Account Number,
    Default Payment Method, Payment Terms, Bank Account, Approver(s)."""
    app = http_request.app
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=422, detail="Please upload an Excel (.xlsx) file."
        )
    contents = await file.read()
    try:
        duplicates = find_existing_supplier_imports(
            io.BytesIO(contents),
            supplier_store=app.state.suppliers_store,
        )
        if duplicates and not replace_existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "One or more supplier companies already exist. "
                        "Confirm before replacing their configured values."
                    ),
                    "duplicates": [asdict(duplicate) for duplicate in duplicates],
                },
            )
        summary = import_supplier_workbook(
            io.BytesIO(contents),
            company_store=app.state.companies_store,
            supplier_store=app.state.suppliers_store,
            approval_matrix_store=app.state.approval_matrix_store,
            supplier_terms_store=app.state.supplier_terms_store,
            default_company=company,
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "imported": summary.imported_count,
        "warnings": summary.warning_count,
        "skipped": summary.skipped_count,
        "rows": [asdict(row) for row in summary.rows],
    }


@router.get("/api/admin/ai-threshold")
def admin_get_ai_threshold(http_request: Request, user: User = Depends(require_role(ROLE_ADMIN))) -> dict[str, float]:
    app = http_request.app
    from app.ai_extraction import confidence_threshold

    return {
        "threshold": confidence_threshold(
            app.state.process_configuration_store.get
        )
    }


@router.put("/api/admin/ai-threshold")
def admin_set_ai_threshold(
    http_request: Request,
    request: ThresholdUpdateRequest, user: User = Depends(require_role(ROLE_ADMIN))
) -> dict[str, float]:
    app = http_request.app
    if not 0.0 <= request.threshold <= 1.0:
        raise HTTPException(status_code=422, detail="Threshold must be between 0.0 and 1.0.")
    app.state.process_configuration_store.set(
        "ai_confidence_threshold", str(request.threshold)
    )
    return {"threshold": request.threshold}


@router.put("/api/admin/irj-configurations/{company_name}")
def admin_set_irj_configuration(
    http_request: Request,
    company_name: str,
    request: IrjConfigurationUpdateRequest,
    user: User = Depends(require_role(ROLE_ADMIN)),
) -> dict[str, object]:
    app = http_request.app
    company = app.state.companies_store.get(company_name)
    if company is None:
        raise HTTPException(
            status_code=404,
            detail=f"Company '{company_name}' was not found.",
        )
    mode = request.mode.strip().casefold()
    if mode not in {"automatic", "manual"}:
        raise HTTPException(
            status_code=422,
            detail="IRJ mode must be 'automatic' or 'manual'.",
        )
    if request.current_irj is not None:
        current_irj = request.current_irj.strip()
        if len(current_irj) != 6 or not current_irj.isdigit():
            raise HTTPException(
                status_code=422,
                detail="The latest paper IRJ must contain exactly six digits.",
            )
        highest = app.state.invoice_store.highest_irj_number(company.name)
        if highest is not None and int(current_irj) < highest:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"The latest paper IRJ cannot be below the highest "
                    f"digitally assigned IRJ ({highest:06d})."
                ),
            )
        app.state.irj_generator.set_current(company.name, current_irj)
    app.state.process_configuration_store.set(
        f"irj_mode:{company.name.strip().casefold()}",
        mode,
    )
    return irj_configuration(app, company)
