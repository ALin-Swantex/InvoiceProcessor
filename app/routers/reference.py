"""Read-only reference data: IRJ configuration, companies, suppliers,
approval matrix and supplier terms."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Request

from app.approval_matrix import ALL_COMPANIES
from app.auth import User, get_current_user
from app.deps import irj_configuration, sync_sharepoint_companies

router = APIRouter()


@router.get("/api/irj-configurations")
def irj_configurations(
    http_request: Request,
    user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    app = http_request.app
    return [
        irj_configuration(app, company)
        for company in app.state.companies_store.list()
    ]


@router.get("/api/companies")
def companies(http_request: Request, user: User = Depends(get_current_user)) -> list[dict[str, str]]:
    app = http_request.app
    sync_sharepoint_companies(app)
    return [
        {
            "name": profile.name,
            "company_folder": profile.company_folder,
            "po_matching_folder": profile.po_matching_folder,
        }
        for profile in app.state.companies_store.list()
    ]


@router.get("/api/suppliers")
def suppliers(
    http_request: Request,
    company: str | None = Query(None),
    user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    app = http_request.app
    profiles = app.state.suppliers_store.list()
    if company:
        company_key = company.strip().casefold()
        configured_suppliers = {
            entry.supplier.strip().casefold()
            for entry in app.state.approval_matrix_store.list()
            if entry.company == ALL_COMPANIES
            or entry.company.strip().casefold() == company_key
        }
        configured_suppliers.update(
            terms.supplier.strip().casefold()
            for terms in app.state.supplier_terms_store.list()
            if terms.company == ALL_COMPANIES
            or terms.company.strip().casefold() == company_key
        )
        profiles = [
            profile
            for profile in profiles
            if (
                profile.default_company is not None
                and profile.default_company.strip().casefold() == company_key
            )
            or profile.name.strip().casefold() in configured_suppliers
        ]
    return [asdict(profile) for profile in profiles]


@router.get("/api/approval-matrix")
def approval_matrix(http_request: Request, user: User = Depends(get_current_user)) -> list[dict[str, object]]:
    app = http_request.app
    return [
        {
            "company": entry.company,
            "supplier": entry.supplier,
            "approver1": {"name": entry.approver1.name, "email": entry.approver1.email},
            "approver2": (
                {"name": entry.approver2.name, "email": entry.approver2.email}
                if entry.approver2
                else None
            ),
        }
        for entry in app.state.approval_matrix_store.list()
    ]


@router.get("/api/supplier-terms")
def supplier_terms(
    http_request: Request,
    company: str,
    supplier: str,
    user: User = Depends(get_current_user),
) -> dict[str, object]:
    app = http_request.app
    profiles = app.state.supplier_terms_store.list_for_supplier(company, supplier)
    return {"profiles": [asdict(terms) for terms in profiles]}
