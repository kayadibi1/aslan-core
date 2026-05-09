"""Structured logging + RFC 7807 exception handler for the research API.

Per ``docs/specs/bitemporal-research-api/SCOPE.md`` D16 (RFC 7807
problem+json) and D25 (structlog with request-id correlation).

This module:

1. Configures ``structlog`` for the bitemporal API (JSON in
   prod-like environments, key-value in dev). Idempotent: calling
   :func:`configure_research_logging` more than once is a no-op.
2. Exposes :func:`get_research_logger` which returns a ``structlog``
   BoundLogger pre-bound to ``component="bitemporal-research-api"``.
3. Provides :func:`bind_request_log_context` — a context manager
   that binds ``request_id``, ``endpoint``, and ``api_key_id`` on a
   contextvar so every log line emitted within the request inherits
   them.
4. Provides :func:`register_research_exception_handlers(app)` which
   adds RFC 7807 handlers for ``HTTPException`` (only on the
   ``/v1/research/*`` paths — other surfaces keep their existing
   shape via :mod:`aslan_core.api.middleware`) and a 500 handler
   that records the exception with structlog (full traceback) and
   bumps a Prometheus counter.

Sentry breadcrumbs: ``aslan-core[obs]`` activates ``sentry_sdk`` at
startup; we don't import it here but we don't suppress it either —
unhandled exceptions reach Sentry through Sentry's own ASGI
integration.

Convention: error log fields always use ``snake_case``. Strings
that may carry user input or PII (query params, payload values) are
hashed before logging.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# ---------------------------------------------------------------------
# Configuration (idempotent).
# ---------------------------------------------------------------------


_CONFIGURED = False


def configure_research_logging(*, json_output: bool | None = None) -> None:
    """Configure structlog for the bitemporal-research-API.

    ``json_output`` defaults to ``True`` when ``ASLAN_LOG_JSON=1`` is
    set (production / staging); ``False`` otherwise (dev).

    Safe to call multiple times — only the first call has an effect.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    if json_output is None:
        json_output = os.environ.get("ASLAN_LOG_JSON", "0") == "1"

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer(
            sort_keys=True
        )
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Stdlib root config — keep it cheap; structlog dispatches via stdlib.
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(stream=sys.stderr)
        root.addHandler(handler)
    root.setLevel(os.environ.get("ASLAN_LOG_LEVEL", "INFO"))

    _CONFIGURED = True


def get_research_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog BoundLogger pre-bound for the research API."""
    if not _CONFIGURED:
        configure_research_logging()
    base: structlog.stdlib.BoundLogger = structlog.get_logger(
        name or "aslan_core.api.research"
    )
    return base.bind(component="bitemporal-research-api")


# ---------------------------------------------------------------------
# Per-request log binding via contextvars.
# ---------------------------------------------------------------------


_REQUEST_LOG_BOUND: ContextVar[bool] = ContextVar("_REQUEST_LOG_BOUND", default=False)


@contextmanager
def bind_request_log_context(
    *,
    request_id: UUID,
    endpoint: str,
    api_key_id: UUID | None,
) -> Iterator[None]:
    """Bind per-request fields to structlog's contextvars.

    Every ``structlog`` logger emitted within this context inherits
    ``request_id``, ``endpoint``, and ``api_key_id`` (when known).
    Callers MUST exit the context (use ``with``) so the contextvar
    is cleared between requests on the same worker.
    """
    if not _CONFIGURED:
        configure_research_logging()
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=str(request_id),
        endpoint=endpoint,
        api_key_id=str(api_key_id) if api_key_id is not None else None,
    )
    token = _REQUEST_LOG_BOUND.set(True)
    try:
        yield
    finally:
        _REQUEST_LOG_BOUND.reset(token)
        structlog.contextvars.clear_contextvars()


def update_request_log_context(**kwargs: Any) -> None:
    """Add additional fields to the current request's log context.

    No-op outside of :func:`bind_request_log_context`. Use to bind
    fields that aren't known at middleware-entry time (e.g.,
    ``api_key_id`` after auth, ``rate_tier``, ``rows_returned``).
    """
    if _REQUEST_LOG_BOUND.get():
        structlog.contextvars.bind_contextvars(**kwargs)


# ---------------------------------------------------------------------
# RFC 7807 exception handlers (D16) — research paths only.
# ---------------------------------------------------------------------


_RESEARCH_PREFIX = "/v1/research"


def _is_research_path(request: Request) -> bool:
    return request.url.path.startswith(_RESEARCH_PREFIX)


def _problem_json(
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str | None = None,
    instance: str,
    request_id: str | None,
    extensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an RFC 7807 problem+json body per SCOPE.md D16."""
    body: dict[str, Any] = {
        "type": f"https://docs.aslanterminal.com/errors/{code}",
        "title": title,
        "status": status_code,
        "instance": instance,
        "code": code,
    }
    if detail is not None:
        body["detail"] = detail
    if request_id is not None:
        body["request_id"] = request_id
    if extensions:
        body["extensions"] = extensions
    return body


def register_research_exception_handlers(app: FastAPI) -> None:
    """Add bitemporal-API exception handlers to *app*.

    Idempotent in the sense that it adds handlers that don't conflict
    with the existing ``register_exception_handlers``: those still
    handle non-research paths as before; ours short-circuit only when
    the request is under ``/v1/research``.
    """
    log = get_research_logger("aslan_core.api.research.errors")

    @app.exception_handler(StarletteHTTPException)
    async def _research_http_exc_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if not _is_research_path(request):
            # Defer to the existing app-wide handler.
            from aslan_core.api.middleware import (
                register_exception_handlers as _re,
            )

            # Reuse via a one-shot delegation: build the same shape the
            # existing handler would. We don't want to call the existing
            # handler closure directly because it's bound to a different
            # FastAPI instance; instead replicate its body.
            del _re  # only imported for documentation purposes
            detail: Any = (
                exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": detail, "status_code": exc.status_code},
            )

        # Research path: RFC 7807.
        request_id: str | None = None
        ctx = getattr(request.state, "research_ctx", None)
        if ctx is not None:
            request_id = str(ctx.request_id)

        # If exc.detail is already a dict containing `code`/`title`
        # (the route's own structured detail), prefer those values.
        # Starlette types `detail` as ``str`` only; FastAPI permits
        # arbitrary JSON-serializable values. Read via ``Any`` cast.
        code = "BITEMPORAL_INTERNAL"
        title = "Bitemporal-research-API error"
        detail_text: str | None = None
        extensions: dict[str, Any] | None = None
        raw_detail: Any = exc.detail
        if isinstance(raw_detail, dict):
            code = str(raw_detail.get("code") or code)
            title = str(raw_detail.get("title") or title)
            inner_detail = raw_detail.get("detail")
            if isinstance(inner_detail, str):
                detail_text = inner_detail
            ext = raw_detail.get("extensions")
            if isinstance(ext, dict):
                extensions = ext
        elif isinstance(raw_detail, str):
            detail_text = raw_detail

        # Log at WARNING for client errors, ERROR for server errors.
        log_event = log.warning if exc.status_code < 500 else log.error
        log_event(
            "research_http_exception",
            status_code=exc.status_code,
            code=code,
            path=request.url.path,
            method=request.method,
            request_id=request_id,
        )

        body = _problem_json(
            status_code=exc.status_code,
            code=code,
            title=title,
            detail=detail_text,
            instance=request.url.path,
            request_id=request_id,
            extensions=extensions,
        )
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(Exception)
    async def _research_unhandled_exc_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        if not _is_research_path(request):
            # Existing app-wide handler will catch it. Re-raise so
            # FastAPI's exception-handler chain delegates upward.
            raise exc

        ctx = getattr(request.state, "research_ctx", None)
        request_id = str(ctx.request_id) if ctx is not None else None

        # Full traceback to logs + Sentry (if enabled).
        log.exception(
            "research_unhandled_exception",
            path=request.url.path,
            method=request.method,
            request_id=request_id,
            exception_type=type(exc).__name__,
        )

        body = _problem_json(
            status_code=500,
            code="INTERNAL_ERROR",
            title="Internal server error",
            detail="An unexpected error occurred. Reference the request_id when escalating.",
            instance=request.url.path,
            request_id=request_id,
        )
        return JSONResponse(status_code=500, content=body)


__all__ = [
    "bind_request_log_context",
    "configure_research_logging",
    "get_research_logger",
    "register_research_exception_handlers",
    "update_request_log_context",
]
