"""FastHTML application factory for the v0.6.0 dashboard.

The app is constructed at module-import time so page modules can
register routes via the standard ``@app.get("/path")`` pattern. The
session factory + Redis client are NOT bound at import time —
``configure_app()`` injects them, and ``serve()`` (Task 12) will
call configure_app from the CLI startup path. Tests call
configure_app with the testcontainer fixtures.

Page modules in ``aslan_core.dashboard.pages.*`` are imported at
the bottom of this module so their decorators run AFTER ``app`` is
defined. Each page module imports ``app`` from this file at
module scope; circular imports are avoided because ``app`` is a
module-level binding before the page imports run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fasthtml.common import FastHTML

from aslan_core.dashboard.redis_probes import RedisCircuitBreaker

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


# ── Module-level state ───────────────────────────────────────────


# ``hdrs=()`` suppresses FastHTML's default head injection. Our
# ``render.py`` base template provides the full <head> + <body>
# scaffolding (sidebar nav, htmx, dashboard.css, favicon). Any
# default htmx CDN / pico CSS injection would conflict with our
# vendored static assets (Task 11).
app: FastHTML = FastHTML(hdrs=())


_session_factory: async_sessionmaker[AsyncSession] | None = None
_redis_client: Redis | None = None
_breaker: RedisCircuitBreaker = RedisCircuitBreaker()


# ── Configuration ────────────────────────────────────────────────


def configure_app(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    """Bind the runtime dependencies to the app.

    Called by ``serve()`` (Task 12) at CLI startup, and by tests
    before issuing any request through ``httpx.AsyncClient``. The
    circuit breaker state is re-used across reconfigurations — its
    sliding window is intentionally process-local."""
    global _session_factory, _redis_client
    _session_factory = session_factory
    _redis_client = redis_client


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError(
            "dashboard app is not configured — call configure_app(...) before issuing a request"
        )
    return _session_factory


def get_redis_client() -> Redis:
    if _redis_client is None:
        raise RuntimeError(
            "dashboard app is not configured — call configure_app(...) before issuing a request"
        )
    return _redis_client


def get_breaker() -> RedisCircuitBreaker:
    return _breaker


# ── Page registration ────────────────────────────────────────────


# Importing the page modules executes their module-level
# ``register_template(...)`` and ``@app.get(...)`` decorators.
# Done at the BOTTOM of this file so ``app`` is fully defined by
# the time the imports fire. Each page module imports ``app`` from
# here; the import is non-circular because the binding above has
# already run.
def _register_pages() -> None:
    # Each module's import triggers its ``@app.get("/path")`` decorator
    # and ``register_template(VMType, builder)`` call. The local-name
    # bindings (``_overview`` etc.) are intentionally unused at this
    # call site — the side effects ARE the registration.
    from aslan_core.dashboard.pages import audit as _audit  # noqa: F401
    from aslan_core.dashboard.pages import deadletter as _deadletter  # noqa: F401
    from aslan_core.dashboard.pages import documents as _documents  # noqa: F401
    from aslan_core.dashboard.pages import errors as _errors  # noqa: F401
    from aslan_core.dashboard.pages import ingestion as _ingestion  # noqa: F401
    from aslan_core.dashboard.pages import outbox as _outbox  # noqa: F401
    from aslan_core.dashboard.pages import overview as _overview  # noqa: F401
    from aslan_core.dashboard.pages import redactions as _redactions  # noqa: F401
    from aslan_core.dashboard.pages import streams as _streams  # noqa: F401
    from aslan_core.dashboard.pages import timeseries as _timeseries  # noqa: F401


_register_pages()


__all__ = ["app", "configure_app", "get_breaker", "get_redis_client", "get_session_factory"]
