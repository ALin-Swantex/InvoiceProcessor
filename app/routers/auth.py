"""Authentication (local + Microsoft 365) and health endpoints."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response as FastAPIResponse
from fastapi.responses import RedirectResponse

from app.auth import AuthError, SESSION_COOKIE_NAME, User, get_current_user
from app.deps import set_session_cookie
from app.schemas import LoginRequest

logger = logging.getLogger("invoice_processor")

router = APIRouter()


@router.get("/api/auth/config")
def auth_config(http_request: Request) -> dict[str, bool]:
    app = http_request.app
    return {
        "microsoft_enabled": app.state.entra_auth_client is not None,
        "local_enabled": app.state.local_login_enabled,
    }


@router.post("/api/auth/login")
def login(http_request: Request, request: LoginRequest, response: FastAPIResponse) -> dict[str, object]:
    app = http_request.app
    if not app.state.local_login_enabled:
        raise HTTPException(
            status_code=404,
            detail="Local password sign-in is disabled.",
        )
    try:
        user = app.state.auth_store.authenticate(request.username, request.password)
    except AuthError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    token = app.state.auth_store.create_session(user.username)
    set_session_cookie(app, response, token)
    return {
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "role": user.role,
    }


@router.get("/api/auth/microsoft/login")
def microsoft_login(http_request: Request) -> RedirectResponse:
    app = http_request.app
    client = app.state.entra_auth_client
    if client is None:
        raise HTTPException(
            status_code=404,
            detail="Microsoft 365 sign-in is not configured.",
        )
    try:
        flow = client.initiate_flow()
        app.state.auth_store.save_oauth_flow(str(flow["state"]), flow)
    except AuthError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return RedirectResponse(str(flow["auth_uri"]), status_code=302)


@router.get("/api/auth/microsoft/callback")
def microsoft_callback(request: Request) -> RedirectResponse:
    app = request.app
    client = app.state.entra_auth_client
    if client is None:
        raise HTTPException(
            status_code=404,
            detail="Microsoft 365 sign-in is not configured.",
        )
    state = request.query_params.get("state", "")
    flow = app.state.auth_store.pop_oauth_flow(state)
    if flow is None:
        raise HTTPException(
            status_code=400,
            detail="The Microsoft sign-in request expired or is invalid.",
        )
    try:
        user = client.complete_flow(flow, request.query_params)
    except AuthError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    token = app.state.auth_store.create_federated_session(user)
    response = RedirectResponse("/", status_code=302)
    set_session_cookie(app, response, token)
    return response


@router.get("/api/auth/microsoft/logout")
def microsoft_logout(request: Request) -> RedirectResponse:
    app = request.app
    client = app.state.entra_auth_client
    if client is None:
        return RedirectResponse("/", status_code=302)
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        app.state.auth_store.delete_session(token)
    response = RedirectResponse(client.logout_url(), status_code=302)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@router.post("/api/auth/logout")
def logout(request: Request, response: FastAPIResponse) -> dict[str, str]:
    app = request.app
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        app.state.auth_store.delete_session(token)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"status": "signed_out"}


@router.get("/api/auth/me")
def me(user: User = Depends(get_current_user)) -> dict[str, object]:
    return {
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "role": user.role,
    }


@router.get("/api/health")
def health(http_request: Request) -> dict[str, object]:
    app = http_request.app
    if app.state.worker_monitor is None:
        return {"status": "ok", "mode": "outlook-intake"}
    try:
        app.state.invoice_store.list(1)
        worker = app.state.worker_monitor.status()
    except Exception as error:
        logger.exception("Health check failed")
        return {
            "status": "unhealthy",
            "mode": "outlook-intake",
            "database": "unavailable",
            "detail": type(error).__name__,
        }
    stale_after = int(os.environ.get("WORKER_STALE_AFTER_SECONDS", "120"))
    worker_stale = worker is None or int(worker["age_seconds"]) > stale_after
    return {
        "status": "degraded" if worker_stale else "ok",
        "mode": "outlook-intake",
        "database": "ok",
        "worker": "stale" if worker_stale else str(worker["status"]),
        "worker_last_seen_at": worker["last_seen_at"] if worker else None,
    }
