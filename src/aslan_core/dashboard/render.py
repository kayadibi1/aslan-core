"""Render helper + base template for the v0.6.0 dashboard.

Spec §6.3 + codex round-2 + plan-round-1: every page handler in
``aslan_core.dashboard.pages`` ends with ``return render(request, vm)``.
The helper:

  1. Runtime-checks that ``vm`` is a ``_VMBase`` subclass — closes the
     hole where a future handler accidentally passes a plain
     ``BaseModel`` (or worse, a SQLAlchemy ``Row``) and a forbidden
     attribute leaks via ``__str__`` / ``__repr__``.

  2. Calls ``vm.model_dump(mode='json')`` BEFORE handing to the
     template. The serialised dict is materialised even if the
     template doesn't consume it directly — exercising
     ``model_dump`` triggers Pydantic's serialiser and surfaces a
     non-JSON-safe field type (``bytes``, ``Any``-typed leaks)
     before any HTML is rendered. The type-allowlist check in
     ``test_dashboard_vm_field_types_are_safe.py`` is the static
     floor; this is the runtime backstop.

  3. Looks up the body builder for the concrete VM type in
     ``_TEMPLATES``. Page modules register their builders via
     ``register_template(VMType, build_body)`` at import time.

  4. Wraps the body in the base template (sidebar nav + htmx +
     stylesheet + favicon) and returns ``HTMLResponse``.

The route-table-wide "every handler ends with ``return render(...)``"
enforcement test lands in Task 15.5 once Tasks 8-12 have populated the
route table — running it now would inspect a near-empty table and
pass trivially.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fasthtml.common import (
    A,
    Body,
    Head,
    Html,
    Li,
    Link,
    Main,
    Nav,
    Script,
    Title,
    Ul,
    to_xml,
)
from starlette.responses import HTMLResponse

from aslan_core.dashboard.static import HTMX_SRI
from aslan_core.dashboard.view_models import _VMBase

if TYPE_CHECKING:
    from starlette.requests import Request


# ── Template registry ────────────────────────────────────────────


# Maps a concrete VM type to a body-builder. The builder takes the
# VM instance and returns a FastHTML element tree (the body of the
# page, NOT the full HTML document — the base template wraps it).
_TEMPLATES: dict[type[_VMBase], Callable[[Any], Any]] = {}


def register_template[T: _VMBase](vm_type: type[T], builder: Callable[[T], Any]) -> None:
    """Register a body builder for ``vm_type``. Called by each page
    module at import time. ``vm_type`` MUST be a concrete subclass
    of ``_VMBase``.

    A second registration for the same type silently overrides the
    first — that's intentional for tests that swap templates."""
    _TEMPLATES[vm_type] = builder


def clear_templates() -> None:
    """Test-only reset hook. Production never calls this."""
    _TEMPLATES.clear()


# ── Base template ────────────────────────────────────────────────


_NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("Overview", "/"),
    ("Outbox", "/outbox"),
    ("Deadletter", "/deadletter"),
    ("Streams", "/streams"),
    ("Ingestion", "/ingestion"),
    ("Documents", "/documents"),
    ("Timeseries", "/timeseries"),
    ("Audit", "/audit"),
    ("Redactions", "/redactions"),
    ("Review", "/review"),
)


def _base_template(*, title: str, body: Any) -> Any:
    """Wrap ``body`` in the dashboard's HTML skeleton — sidebar nav,
    htmx, stylesheet, favicon. Returns a FastHTML element tree.

    The htmx ``<script>`` tag carries an SRI ``integrity`` attribute
    so a future re-vendor cannot silently swap the file. The hash is
    computed from the bytes on disk at module-import time
    (``aslan_core.dashboard.static.HTMX_SRI``) and verified against
    the ``htmx.min.js.sha256`` sidecar — a mismatch fails app startup
    rather than serving a hash-mismatched script."""
    return Html(
        Head(
            Title(f"{title} · aslan dashboard"),
            Link(rel="stylesheet", href="/static/dashboard.css"),
            Link(rel="icon", href="/static/favicon.ico"),
            Script(
                src="/static/htmx.min.js",
                integrity=HTMX_SRI,
                crossorigin="anonymous",
            ),
        ),
        Body(
            Nav(
                Ul(*[Li(A(label, href=path)) for label, path in _NAV_ITEMS]),
                cls="aslan-nav",
            ),
            Main(body, cls="aslan-main"),
        ),
    )


# ── Render helper ────────────────────────────────────────────────


def render(request: Request, vm: _VMBase) -> HTMLResponse:
    """Render ``vm`` to an HTMLResponse via the registered template.

    Spec contract:

      * ``isinstance(vm, _VMBase)`` — runtime check; rejects plain
        ``BaseModel`` and non-pydantic objects.
      * ``vm.model_dump(mode='json')`` is called — surfaces non-JSON-
        safe types at runtime.
      * No implicit ``str(vm)`` / ``repr(vm)`` on any path. The body
        builder receives the typed VM and projects fields explicitly.

    ``request`` is currently unused but kept in the signature so the
    ``return render(request, vm)`` shape is uniform with FastHTML
    handler conventions. Future use: htmx-partial detection via
    ``request.headers.get('HX-Request')``.
    """
    if not isinstance(vm, _VMBase):
        raise TypeError(
            f"render() requires a _VMBase subclass; "
            f"got {type(vm).__name__}. Page handlers MUST construct a "
            f"dashboard view model — never pass a SQLAlchemy Row or a "
            f"plain dict."
        )
    # Materialise to JSON-safe shape — surfaces a forbidden type at
    # runtime even if the static check (test_dashboard_vm_field_types_are_safe)
    # has been bypassed.
    vm.model_dump(mode="json")

    builder = _TEMPLATES.get(type(vm))
    if builder is None:
        raise LookupError(
            f"no template registered for {type(vm).__name__}. "
            f"Page modules must call register_template(VMType, build_body) "
            f"at import time."
        )

    body = builder(vm)
    title = type(vm).__name__.removesuffix("VM") or "Aslan"
    page = _base_template(title=title, body=body)
    html = to_xml(page)
    return HTMLResponse(html)


__all__ = ["clear_templates", "register_template", "render"]
