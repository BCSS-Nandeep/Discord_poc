"""Serves the local browser console.

The console is a single static HTML file served from the application itself, which
matters for two reasons:

* **Same origin.** It calls ``/discord/*`` directly, so no CORS configuration is needed
  and it works with ``CORS_ORIGINS`` left empty.
* **No build step and no CDN.** All CSS and JS are inline, so the console works on an
  air-gapped machine and adds no dependency to the service.

It is a thin client: it holds no state and contains no business rules, only fetch calls
against the documented REST API.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.exceptions import NotFoundError

router = APIRouter(tags=["console"])

UI_FILE = Path(__file__).resolve().parent.parent / "ui" / "index.html"


@router.get(
    "/ui",
    response_class=HTMLResponse,
    summary="Browser console",
    description=(
        "A single-page console for driving this API by hand: server and channel "
        "discovery, the private-channel access workflow, collection, search, monitors "
        "and notifications. Every action maps to one documented REST call, and the "
        "page shows which one."
    ),
    include_in_schema=False,
)
async def console() -> HTMLResponse:
    """Return the console page."""

    if not UI_FILE.is_file():  # pragma: no cover - only if the file is deleted
        raise NotFoundError(
            "Console asset is missing from this installation",
            details={"expected": str(UI_FILE)},
        )
    # Read per request rather than at import: the file is small, and this means an
    # edit shows up on refresh without restarting the service.
    return HTMLResponse(UI_FILE.read_text(encoding="utf-8"))


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Send the bare host to the console rather than a 404."""

    return RedirectResponse(url="/ui")
