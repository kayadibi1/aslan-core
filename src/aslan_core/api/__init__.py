"""Aslan Financial API — FastAPI application factory.

Usage::

    from aslan_core.api import create_api_app
    app = create_api_app()
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from aslan_core.db.engine import create_engine

# Resolved at import time — safe to use synchronous pathlib outside any
# async context (ASYNC240 only fires for Path usage inside async functions).
_CANONICAL_LINES_MANIFEST = Path(__file__).resolve().parents[1] / "canonical_lines.yaml"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the DB engine once at startup; dispose on shutdown."""
    # Configure structured logging once before any request fires.
    from aslan_core.api.research_logging import configure_research_logging

    configure_research_logging()

    engine = create_engine()
    app.state.engine = engine

    # Pre-load canonical lines manifest if the file exists.
    from aslan_core.query.catalog import load_canonical_lines
    from aslan_core.query.schemas import CanonicalLineInfo

    lines: list[CanonicalLineInfo] = []
    if _CANONICAL_LINES_MANIFEST.exists():
        lines = load_canonical_lines(_CANONICAL_LINES_MANIFEST)
    app.state.canonical_lines = lines
    app.state.canonical_codes = {li.canonical_code for li in lines}

    yield

    await engine.dispose()


def create_api_app() -> FastAPI:
    """Build and return the configured FastAPI application.

    Route and middleware modules are imported lazily inside this factory so
    that mypy does not follow them at module-analysis time.  FastAPI's route
    decorators are not fully typed under strict mode; analysis is deferred to
    the integration test suite.
    """
    # Lazy imports — keep inside the function body so mypy skips them when
    # analysing this module directly.
    from starlette.middleware.base import BaseHTTPMiddleware

    from aslan_core.api.middleware import register_exception_handlers
    from aslan_core.api.research_logging import register_research_exception_handlers
    from aslan_core.api.research_observability import research_audit_middleware
    from aslan_core.api.routes.auth import router as auth_router
    from aslan_core.api.routes.catalog import router as catalog_router
    from aslan_core.api.routes.entities import router as entities_router
    from aslan_core.api.routes.financials import router as financials_router
    from aslan_core.api.routes.research import router as research_router

    app = FastAPI(
        title="Aslan Financial API",
        version="0.10.0",
        lifespan=_lifespan,
    )

    app.include_router(auth_router, prefix="/auth", tags=["auth"])
    app.include_router(entities_router, prefix="/entities", tags=["entities"])
    app.include_router(financials_router, prefix="/financials", tags=["financials"])
    app.include_router(catalog_router, tags=["catalog"])
    # Bitemporal Research API — gated by BITEMPORAL_API_ENABLED feature flag.
    # Mounts at /v1/research; the router defines its own prefix internally.
    app.include_router(research_router)

    # Research-API cross-cutting middleware (D18 audit, D25 metrics).
    # Filters by path inside the dispatcher, so it is safe to add globally.
    app.add_middleware(BaseHTTPMiddleware, dispatch=research_audit_middleware)

    # Research-API exception handlers (RFC 7807 per D16, structured
    # error logging per D25). FastAPI keeps ONE handler per exception
    # class; the LAST registration wins. So generic handlers register
    # first, research handlers second — research path-checks then
    # delegate back to a generic-shaped response for non-research
    # URLs internally.
    register_exception_handlers(app)
    register_research_exception_handlers(app)

    return app


__all__ = ["create_api_app"]
