"""Custom exception handlers for uniform API error responses.

All errors are returned as ``{"detail": "...", "status_code": N}`` so
consumers never see FastAPI's default 422 shape or raw stack traces.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach custom exception handlers to *app*."""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        detail: Any = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail, "status_code": exc.status_code},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = " -> ".join(str(part) for part in first.get("loc", []))
            msg = first.get("msg", "Validation error")
            detail = f"{loc}: {msg}" if loc else msg
        else:
            detail = "Validation error"
        return JSONResponse(
            status_code=422,
            content={"detail": detail, "status_code": 422},
        )

    @app.exception_handler(Exception)
    async def _generic_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "status_code": 500},
        )


__all__ = ["register_exception_handlers"]
