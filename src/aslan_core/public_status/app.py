"""FastHTML application factory for the public ``/status`` page.

Sibling to :mod:`aslan_core.dashboard.app` but with NO auth, NO Redis,
NO htmx static assets, NO compliance banner — just a single static-
rendered page at ``/status``. Configured via ``configure_app()``
which injects the session factory bound to the
``public_status_reader`` PostgreSQL role.

The app deliberately has NO middleware mirror of the internal
dashboard's ``DashboardMetricsMiddleware``. Public-facing status pages
are typically fronted by a CDN; per-request metrics are best collected
at the edge / reverse proxy. A future deployment that wants Prometheus
on the origin can mount it via a sidecar without invasive code change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fasthtml.common import FastHTML

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


# ── Module-level state ───────────────────────────────────────────


# ``hdrs=()`` suppresses FastHTML's default head injection. Our
# ``render.py`` provides the full <head> + <body> scaffolding.
app: FastHTML = FastHTML(hdrs=())


_session_factory: async_sessionmaker[AsyncSession] | None = None


# ── Configuration ────────────────────────────────────────────────


def configure_app(
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bind the runtime session factory to the app.

    Called by ``serve()`` at CLI startup, and by tests before issuing
    any request through ``httpx.AsyncClient``. The session factory
    MUST be backed by an engine logged in as ``public_status_reader``
    so the privilege boundary is exercised on every request.
    """
    global _session_factory
    _session_factory = session_factory


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError(
            "public_status app is not configured — call configure_app(...) before issuing a request"
        )
    return _session_factory


# ── Page registration ────────────────────────────────────────────


def _register_pages() -> None:
    # Importing the page module triggers its ``@app.get("/status")``
    # decorator. Done at the BOTTOM of this file so ``app`` is fully
    # defined by the time the import fires.
    from aslan_core.public_status.pages import status as _status  # noqa: F401


_register_pages()


__all__ = ["app", "configure_app", "get_session_factory"]
