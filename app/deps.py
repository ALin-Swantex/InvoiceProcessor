"""Shared helpers for the API routers.

These used to be closures inside ``create_app``; they now read everything
from ``app.state`` so each router module can use them with ``request.app``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Response as FastAPIResponse

from app.auth import (
    ROLE_ADMIN,
    ROLE_APPROVER_1,
    ROLE_APPROVER_2,
    SESSION_COOKIE_NAME,
    User,
)
from app.companies import CompanyProfile
from app.company_folders import (
    CompanyFolderStructure,
    discover_company_folder_structures,
)
from app.invoice_lifecycle import InvoiceLifecycle, InvoiceLifecycleError
from app.invoices import InvoiceRecord
from app.sharepoint import SharePointClient, SharePointError

logger = logging.getLogger("invoice_processor")


def get_lifecycle(app: FastAPI) -> InvoiceLifecycle:
    # Lazily attach a SharePoint client from the environment the first
    # time it is needed, so tests that never touch SharePoint never pay
    # for constructing one, but real deployments pick up .env values
    # without an explicit override. Only attempt this once per process
    # -- if SharePoint isn't configured, retrying on every request would
    # just repeat the same failure silently.
    current: InvoiceLifecycle = app.state.lifecycle
    if (
        current.sharepoint_client is None
        and app.state.sharepoint_client is None
        and not app.state.sharepoint_attach_attempted
    ):
        app.state.sharepoint_attach_attempted = True
        try:
            from app.sharepoint import sharepoint_client_from_environment

            client = sharepoint_client_from_environment()
            app.state.sharepoint_client = client
            current.sharepoint_client = client
            logger.info("SharePoint client configured; invoice PDFs will be filed centrally.")
        except Exception as error:
            logger.info(
                "SharePoint filing disabled (%s: %s). Running in local test mode -- "
                "invoices will complete their workflow locally without being filed "
                "in SharePoint.",
                type(error).__name__,
                error,
            )
    return current


def require_sharepoint_client(app: FastAPI) -> SharePointClient:
    client = get_lifecycle(app).sharepoint_client
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="SharePoint is not configured or accessible.",
        )
    return client


def lifecycle_action(action: Callable[..., InvoiceRecord], *args: object, **kwargs: object) -> dict[str, object]:
    """Run a lifecycle step, reporting a workflow error as HTTP 422."""
    try:
        record = action(*args, **kwargs)
    except InvoiceLifecycleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return asdict(record)


def sync_sharepoint_companies(app: FastAPI) -> list[CompanyProfile]:
    """Import complete SharePoint company folders into the company store."""
    if app.state.sharepoint_company_sync_complete:
        return []
    client = get_lifecycle(app).sharepoint_client
    if client is None:
        return []
    try:
        structures = discover_company_folder_structures(
            client.list_folder_paths()
        )
    except SharePointError as error:
        logger.warning("SharePoint company synchronization failed: %s", error)
        return []

    existing = app.state.companies_store.list()
    existing_names = {profile.name.strip().casefold() for profile in existing}
    existing_roots = {
        profile.sharepoint_root_folder.strip().casefold()
        for profile in existing
    }
    created: list[CompanyProfile] = []
    for structure in structures:
        name = structure.root.rsplit("/", 1)[-1]
        if (
            name.casefold() in existing_names
            or structure.root.casefold() in existing_roots
        ):
            continue
        try:
            profile = app.state.companies_store.create(
                name=name,
                sharepoint_root_folder=structure.root,
            )
        except ValueError:
            # Another worker may have inserted the same company.
            continue
        created.append(profile)
        existing_names.add(profile.name.casefold())
        existing_roots.add(profile.sharepoint_root_folder.casefold())
    app.state.sharepoint_company_sync_complete = True
    if created:
        logger.info(
            "Imported %d company folder(s) from SharePoint into configuration.",
            len(created),
        )
    return created


def company_response(profile: CompanyProfile) -> dict[str, object]:
    structure = CompanyFolderStructure.from_root(profile.sharepoint_root_folder)
    return {
        **asdict(profile),
        "folder_structure": structure.as_dict(),
    }


def cookies_are_secure(app: FastAPI) -> bool:
    secure_setting = os.environ.get("AUTH_COOKIE_SECURE", "").strip()
    return (
        secure_setting.casefold() in {"1", "true", "yes", "on"}
        if secure_setting
        else bool(app.state.entra_auth_client)
    )


def set_session_cookie(app: FastAPI, response: FastAPIResponse, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=cookies_are_secure(app),
        samesite="lax",
        max_age=12 * 60 * 60,
    )


def irj_configuration(app: FastAPI, company: CompanyProfile) -> dict[str, object]:
    default_mode = (
        "manual"
        if company.name.strip().casefold() in {"swan", "cel"}
        else "automatic"
    )
    mode = app.state.process_configuration_store.get(
        f"irj_mode:{company.name.strip().casefold()}",
        default_mode,
    )
    return {
        "company": company.name,
        "mode": mode,
        "current_irj": app.state.irj_generator.current(company.name),
    }


def normalized_email(value: str | None) -> str:
    return (value or "").strip().casefold()


def invoice_is_visible_to_user(
    invoice: InvoiceRecord,
    user: User,
) -> bool:
    """Limit approvers to invoices assigned to their Entra identity."""
    if user.role not in {ROLE_APPROVER_1, ROLE_APPROVER_2}:
        return True
    user_email = normalized_email(user.email)
    if not user_email:
        return False
    assigned_email = (
        invoice.approver1_email
        if user.role == ROLE_APPROVER_1
        else invoice.approver2_email
    )
    return normalized_email(assigned_email) == user_email


def invoice_for_user(app: FastAPI, invoice_id: int, user: User) -> InvoiceRecord:
    invoice = app.state.invoice_store.get(invoice_id)
    if invoice is None or not invoice_is_visible_to_user(invoice, user):
        # Use the same response for absent and inaccessible records so an
        # approver cannot discover another approver's invoice IDs.
        raise HTTPException(status_code=404, detail="Invoice was not found.")
    return invoice


def approver_scope(user: User) -> tuple[int, str] | None:
    """(level, email) when the user is an approver, else None."""
    if user.role == ROLE_APPROVER_1:
        return (1, user.email)
    if user.role == ROLE_APPROVER_2:
        return (2, user.email)
    return None


def invoices_for_user(app: FastAPI, user: User, limit: int) -> list[InvoiceRecord]:
    """Everything still in progress plus the newest `limit` finished invoices."""
    scope = approver_scope(user)
    if scope is not None and not normalized_email(user.email):
        return []
    return app.state.invoice_store.list_workqueue(limit, scope)


def require_assigned_approver(
    app: FastAPI,
    invoice_id: int,
    user: User,
    level: int,
) -> InvoiceRecord:
    invoice = app.state.invoice_store.get(invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice was not found.")
    if user.role == ROLE_ADMIN:
        return invoice
    expected_role = ROLE_APPROVER_1 if level == 1 else ROLE_APPROVER_2
    assigned_email = (
        invoice.approver1_email if level == 1 else invoice.approver2_email
    )
    if (
        user.role != expected_role
        or not normalized_email(user.email)
        or normalized_email(user.email) != normalized_email(assigned_email)
    ):
        # Same response as a missing invoice (see invoice_for_user).
        raise HTTPException(status_code=404, detail="Invoice was not found.")
    return invoice
