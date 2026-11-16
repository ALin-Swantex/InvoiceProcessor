"""Single-page web UI."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def prototype_home() -> HTMLResponse:
    import app.main as main_module

    # The shell is small and references /static assets that revalidate via
    # ETag, so always revalidate the HTML too; the body is served from an
    # in-process cache keyed on index.html's mtime.
    return HTMLResponse(
        main_module._index_html(), headers={"Cache-Control": "no-cache"}
    )
