"""Supplier statement library (SharePoint)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from app.auth import User, get_current_user
from app.deps import get_lifecycle
from app.pdf_validation import InvalidPdfError, validate_pdf
from app.sharepoint import SharePointError

router = APIRouter()


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
        return client.list_statement_library()
    except SharePointError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


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
        library = client.list_statement_library()
        known_statement_ids = {
            str(statement["id"])
            for statements in library.values()
            for statement in statements
        }
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
