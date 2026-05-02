"""404 + 500 handlers — Task 10.

Spec §6.3 finding 5: error pages return generic responses through
the same ``render(request, vm)`` helper as production pages.

* The 404 handler renders a static ``NotFoundVM`` with no caller-
  supplied path interpolation. Echoing the requested URL would turn
  the 404 surface into a self-XSS vector and a probing channel for
  any data the operator embedded in the URL.

* The 500 handler renders a static ``ServerErrorVM`` with a fresh
  ``incident_id`` (UUID4 — see note below) and no exception text.
  The actual exception goes to the module's structured logger so
  on-call can grep ``incident_id=<uuid>``. Without this, a SELECT
  that hit the wrong column could embed the value in a SQLAlchemy
  error message that bubbles into the rendered HTML.

Both handlers go through ``register_template(...)`` + ``render(...)``
so the dashboard chrome (sidebar nav, htmx, stylesheet) wraps the
page — operators land on a recognisable surface even when they
typo a route or trip a bug.

Note on UUIDv7 vs UUID4: spec §6.3 calls for "fresh UUIDv7"; the
codebase has no UUIDv7 dependency yet (``uuid_utils`` was discussed
in v0.5.0 plans but never landed). The incident_id is opaque, log-
correlation-only — time-ordering buys nothing here. UUID4 is the
right call until a UUIDv7 helper appears project-wide; switching
later is a one-line change.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from fasthtml.common import H1, Div, P
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from aslan_core.dashboard.app import app
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import NotFoundVM, ServerErrorVM

_log = logging.getLogger(__name__)


# ── Templates ────────────────────────────────────────────────────


def _build_not_found_body(vm: NotFoundVM) -> object:
    return Div(
        H1(vm.title),
        P("The page you requested is not part of the dashboard."),
        cls="aslan-error",
    )


def _build_server_error_body(vm: ServerErrorVM) -> object:
    """Show only the incident_id. The operator quotes it to on-call;
    the exception text is in the structured log keyed on the same
    UUID."""
    return Div(
        H1(vm.title),
        P("An internal error occurred. Quote the incident id when reporting."),
        P(f"incident_id: {vm.incident_id}"),
        cls="aslan-error",
    )


register_template(NotFoundVM, _build_not_found_body)
register_template(ServerErrorVM, _build_server_error_body)


# ── Handlers ─────────────────────────────────────────────────────


async def not_found_handler(request: Request, exc: Exception) -> Response:
    """Render the static 404 page. ``request`` is intentionally
    unused — interpolating any field of it would defeat the no-echo
    contract (``request.url`` carries the operator-supplied path)."""
    _ = request
    _ = exc
    response = render(request, NotFoundVM())
    return HTMLResponse(content=response.body, status_code=404)


async def server_error_handler(request: Request, exc: Exception) -> Response:
    """Render the static 500 page with a fresh incident_id and log
    the exception under that id. Re-raising the exception is NOT
    safe here — Starlette's default 500 surface includes the
    exception repr."""
    incident_id = uuid4()
    _log.error(
        "dashboard_unhandled_exception",
        extra={
            "incident_id": str(incident_id),
            "path": request.url.path,
            "exc_type": type(exc).__name__,
        },
        exc_info=exc,
    )
    response = render(request, ServerErrorVM(incident_id=incident_id))
    return HTMLResponse(content=response.body, status_code=500)


# ── Wiring ───────────────────────────────────────────────────────


# Starlette routes 404 through both ``HTTPException(status_code=404)``
# and the route-not-found path. Registering the int 404 catches the
# explicit-raise shape; ``Exception`` catches every other unhandled
# error (including TypeError from a misuse of render(...)).
app.add_exception_handler(404, not_found_handler)
app.add_exception_handler(Exception, server_error_handler)
