"""Single-page web UI."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def prototype_home() -> HTMLResponse:
    import app.main as main_module

    return HTMLResponse(main_module._index_html())
