"""Supplier statement library (SharePoint)."""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response

from app.auth import User, get_current_user
from app.deps import get_lifecycle
from app.pdf_validation import InvalidPdfError, validate_pdf
from app.sharepoint import SharePointError

router = APIRouter()

# How long a statement-library listing is reused to validate PDF ids.
STATEMENT_ID_CACHE_TTL_SECONDS = 60.0
_STATEMENT_ID_CACHE_LOCK = threading.Lock()


def _statement_ids(library: dict[str, list[dict[str, object]]]) -> frozenset[str]:
    return frozenset(
        str(statement["id"])
        for statements in library.values()
        for statement in statements
    )


def _remember_statement_ids(app: FastAPI, client: object, ids: frozenset[str]) -> None:
    with _STATEMENT_ID_CACHE_LOCK:
        app.state.statement_id_cache = (client, time.monotonic(), ids)


def _cached_statement_ids(app: FastAPI, client: object) -> frozenset[str] | None:
    with _STATEMENT_ID_CACHE_LOCK:
        cached = getattr(app.state, "statement_id_cache", None)
    if cached is None:
        return None
    cached_client, cached_at, ids = cached
    if cached_client is not client:
        return None
    if time.monotonic() - cached_at > STATEMENT_ID_CACHE_TTL_SECONDS:
        return None
    return ids


@router.get("/api/statements")
def list_statements(
    http_request: Request,
    user: User = Depends(get_current_user),
) -> dict[str, list[dict[str, object]]]:
    app = http_request.app
    client = get_lifecycle(app).sharepoint_client
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="SharePoint is not configured or accessible.",
        )
    try:
        library = client.list_statement_library()
    except SharePointError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    # Users list statements and then open one, so prime the id cache.
    _remember_statement_ids(app, client, _statement_ids(library))
    return library


@router.get("/api/statements/{item_id}/pdf")
def get_statement_pdf(
    http_request: Request,
    item_id: str,
    user: User = Depends(get_current_user),
) -> Response:
    app = http_request.app
    client = get_lifecycle(app).sharepoint_client
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="SharePoint is not configured or accessible.",
        )
    try:
        # Validating the id needs the whole statement library (up to
        # thousands of items), so reuse a recent listing. A miss always
        # falls back to a fresh listing so newly filed statements open.
        known_statement_ids = _cached_statement_ids(app, client)
        if known_statement_ids is None or item_id not in known_statement_ids:
            known_statement_ids = _statement_ids(client.list_statement_library())
            _remember_statement_ids(app, client, known_statement_ids)
        if item_id not in known_statement_ids:
            raise HTTPException(
                status_code=404,
                detail="Statement PDF was not found.",
            )
        content = client.download_item(item_id)
        validate_pdf(content)
    except HTTPException:
        raise
    except (SharePointError, InvalidPdfError) as error:
        raise HTTPException(
            status_code=502,
            detail=f"Statement PDF could not be loaded: {error}",
        ) from error
    return Response(content=content, media_type="application/pdf")
