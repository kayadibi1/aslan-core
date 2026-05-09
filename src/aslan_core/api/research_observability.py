"""Cross-cutting concerns for the bitemporal research API.

Implements the wiring for SCOPE.md decisions:

- **D6** rate-limit token bucket via ``aslan_core.api_rate_limit_state``.
- **D7** opaque cursor pagination (encode/decode + filters-hash check).
- **D17** Cache-Control + ETag headers (immutable for past as_of,
  no-cache for current).
- **D18** per-request audit log row in ``aslan_core.api_query_audit``.
- **D25** Prometheus metrics: request counter / duration histogram /
  audit insert counter / rate-limit throttle counter / PII access
  counter.

Wiring conventions:

- :class:`RequestContext` is attached to ``request.state.research_ctx``
  by :func:`open_request_context`. It accumulates audit + cache state
  across the request.
- Each route handler calls :func:`enforce_rate_limit` (Depends) and
  passes its as_of-resolved value to :func:`finalize_response` so the
  cache headers and audit row are populated.
- :func:`research_audit_middleware` runs after the route returns,
  writes the audit row, and emits Prometheus metrics.

Prometheus is imported lazily; if ``aslan-core[obs]`` is not
installed the metric calls become no-ops so the module is safe to
import in any environment.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.api.deps import get_session
from aslan_core.api.research_auth import ApiKeyPrincipal, get_principal
from aslan_core.api.research_logging import (
    bind_request_log_context,
    get_research_logger,
    update_request_log_context,
)

logger = get_research_logger("aslan_core.api.research.observability")


# ---------------------------------------------------------------------
# Prometheus metrics (D25). No-op fallback when prometheus_client absent.
# ---------------------------------------------------------------------

_REQ_TOTAL: Any
_REQ_DURATION: Any
_AUDIT_TOTAL: Any
_RATE_THROTTLES: Any
_PII_ACCESS: Any

try:  # pragma: no cover - optional dep
    from prometheus_client import Counter, Histogram

    _METRICS_ENABLED = True

    _REQ_TOTAL = Counter(
        "bitemporal_api_request_total",
        "Total bitemporal-research-API requests.",
        ["endpoint", "status", "rate_tier"],
    )
    _REQ_DURATION = Histogram(
        "bitemporal_api_request_duration_seconds",
        "Bitemporal-research-API request latency.",
        ["endpoint"],
        buckets=(
            0.005, 0.010, 0.025, 0.050, 0.100, 0.200, 0.500, 1.0, 2.5, 5.0, 10.0,
        ),
    )
    _AUDIT_TOTAL = Counter(
        "bitemporal_api_audit_log_inserts_total",
        "Total audit_log rows inserted.",
        ["endpoint"],
    )
    _RATE_THROTTLES = Counter(
        "bitemporal_api_rate_limit_throttles_total",
        "Requests rejected with 429.",
        ["api_key_id", "window_kind"],
    )
    _PII_ACCESS = Counter(
        "bitemporal_api_pii_access_total",
        "PII fields surfaced unredacted (D30).",
        ["api_key_id", "endpoint"],
    )
except ImportError:  # pragma: no cover
    _METRICS_ENABLED = False

    class _NoOp:
        """No-op stand-in when ``prometheus_client`` is not installed."""

        def labels(self, *_args: Any, **_kwargs: Any) -> _NoOp:
            return self

        def inc(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def observe(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    _REQ_TOTAL = _REQ_DURATION = _AUDIT_TOTAL = _RATE_THROTTLES = _PII_ACCESS = _NoOp()


# ---------------------------------------------------------------------
# Request context — accumulator for audit/cache/metric state.
# ---------------------------------------------------------------------


@dataclass
class RequestContext:
    """Per-request state carried on ``request.state.research_ctx``.

    Populated incrementally by dependencies and the route handler;
    consumed by :func:`research_audit_middleware` after the response is
    produced.
    """

    request_id: uuid.UUID = field(default_factory=uuid.uuid4)
    started_monotonic: float = field(default_factory=time.monotonic)
    endpoint: str = ""
    api_key_id: uuid.UUID | None = None
    rate_tier: str = "anonymous"
    as_of_requested: datetime | None = None
    as_of_resolved: datetime | None = None
    rows_returned: int = 0
    feature_flags_active: list[str] = field(default_factory=list)
    pii_unredacted_used: bool = False


def get_request_context(request: Request) -> RequestContext:
    """FastAPI dep: lazily create or return the per-request context."""
    ctx = getattr(request.state, "research_ctx", None)
    if ctx is None:
        ctx = RequestContext(endpoint=f"{request.method} {request.url.path}")
        request.state.research_ctx = ctx
    return ctx


# ---------------------------------------------------------------------
# Cursor pagination (D7).
# ---------------------------------------------------------------------


_CURSOR_VERSION = 1


def filters_hash(filters: dict[str, Any]) -> str:
    """Stable hash of a filter dict; used to detect cursor reuse across
    different filter sets (D7).

    JSON-canonicalizes (sorted keys) then hashes with SHA-256.
    """
    canonical = json.dumps(filters, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:32]


def encode_cursor(*, as_of: datetime, anchor: dict[str, Any], filters: dict[str, Any]) -> str:
    """Encode an opaque base64 cursor.

    The cursor carries the as_of plus the natural-key anchor of the
    last row returned, so the next page resumes deterministically.
    """
    payload = {
        "v": _CURSOR_VERSION,
        "as_of": as_of.isoformat(),
        "anchor": anchor,
        "filters_hash": filters_hash(filters),
    }
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, current_filters: dict[str, Any]) -> dict[str, Any]:
    """Decode the cursor and validate filter compatibility.

    Raises :class:`HTTPException` 400 on malformed cursor or filter
    mismatch.
    """
    try:
        # Re-pad before urlsafe-b64-decoding.
        pad_len = (-len(cursor)) % 4
        raw = base64.urlsafe_b64decode(cursor + ("=" * pad_len))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "BITEMPORAL_INTERVAL_INVALID", "title": "Malformed cursor"},
        ) from exc

    if payload.get("v") != _CURSOR_VERSION:
        raise HTTPException(
            status_code=400,
            detail={"code": "BITEMPORAL_INTERVAL_INVALID", "title": "Unsupported cursor version"},
        )
    if payload.get("filters_hash") != filters_hash(current_filters):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "BITEMPORAL_INTERVAL_INVALID",
                "title": "Cursor filter-set differs from request filters",
            },
        )
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail={"code": "BITEMPORAL_INTERVAL_INVALID", "title": "Cursor payload not an object"},
        )
    return dict(payload)


# ---------------------------------------------------------------------
# Cache headers (D17).
# ---------------------------------------------------------------------


_IMMUTABLE_THRESHOLD_SECONDS = 5 * 60  # 5-minute window absorbs late-arriving extractions.
_IMMUTABLE_MAX_AGE = 31_536_000  # one year


def set_cache_headers(response: Response, *, as_of_resolved: datetime, body_bytes: bytes) -> None:
    """Apply Cache-Control + ETag per SCOPE.md D17.

    Past `as_of` (older than 5 minutes): immutable cache.
    Recent / current: no-cache + ETag for revalidation.
    """
    now = datetime.now(tz=UTC)
    age = (now - as_of_resolved).total_seconds()
    etag = '"' + hashlib.sha256(body_bytes).hexdigest()[:32] + '"'
    response.headers["ETag"] = etag
    if age >= _IMMUTABLE_THRESHOLD_SECONDS:
        response.headers["Cache-Control"] = (
            f"public, max-age={_IMMUTABLE_MAX_AGE}, immutable"
        )
    else:
        response.headers["Cache-Control"] = "no-cache, must-revalidate"


# ---------------------------------------------------------------------
# Rate limiting (D6).
# ---------------------------------------------------------------------


_TIER_LIMITS = {
    # tier_name: {window_kind: limit}
    "internal": {"minute": 10_000, "hour": 100_000, "day": 0},  # 0 = unlimited
    "partner":  {"minute": 600,    "hour": 5_000,   "day": 30_000},
    "public":   {"minute": 60,     "hour": 500,    "day": 2_000},
}


def _window_start(now: datetime, kind: str) -> datetime:
    if kind == "minute":
        return now.replace(second=0, microsecond=0)
    if kind == "hour":
        return now.replace(minute=0, second=0, microsecond=0)
    if kind == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unknown window kind {kind!r}")


async def enforce_rate_limit(
    request: Request,
    principal: ApiKeyPrincipal | None = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
    ctx: RequestContext = Depends(get_request_context),
) -> ApiKeyPrincipal | None:
    """FastAPI dep: token-bucket check on aslan_core.api_rate_limit_state.

    Public endpoints (principal is None) are not rate-limited in v1.
    """
    if principal is None:
        return None

    ctx.api_key_id = principal.key_id
    ctx.rate_tier = principal.rate_tier

    limits = _TIER_LIMITS.get(principal.rate_tier, _TIER_LIMITS["partner"]).copy()
    # Per-key overrides via aslan_core.api_key.rate_overrides (jsonb).
    override_row = (
        await session.execute(
            text("SELECT rate_overrides FROM aslan_core.api_key WHERE key_id = :id"),
            {"id": str(principal.key_id)},
        )
    ).first()
    if override_row and override_row.rate_overrides:
        limits.update(
            {k: int(v) for k, v in (override_row.rate_overrides or {}).items() if k in limits}
        )

    now = datetime.now(tz=UTC)
    for window_kind, cap in limits.items():
        if cap == 0:
            continue
        window_start = _window_start(now, window_kind)
        bucket = (
            await session.execute(
                text(
                    "INSERT INTO aslan_core.api_rate_limit_state "
                    "(api_key_id, window_kind, window_start, tokens_used) "
                    "VALUES (:id, :kind, :start, 1) "
                    "ON CONFLICT (api_key_id, window_kind, window_start) DO UPDATE "
                    "SET tokens_used = api_rate_limit_state.tokens_used + 1, "
                    "    last_request_at = now() "
                    "RETURNING tokens_used"
                ),
                {"id": str(principal.key_id), "kind": window_kind, "start": window_start},
            )
        ).first()
        if bucket and bucket.tokens_used > cap:
            await session.commit()
            _RATE_THROTTLES.labels(
                api_key_id=str(principal.key_id), window_kind=window_kind
            ).inc()
            retry_after = _retry_after_seconds(window_kind, now)
            logger.warning(
                "research_rate_limit_throttle",
                api_key_id=str(principal.key_id),
                rate_tier=principal.rate_tier,
                window_kind=window_kind,
                tokens_used=bucket.tokens_used,
                cap=cap,
                retry_after_seconds=retry_after,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "RATE_LIMITED",
                    "title": (
                        f"Rate limit exceeded for tier {principal.rate_tier} "
                        f"(window={window_kind})"
                    ),
                    "retry_after_seconds": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Tier": principal.rate_tier,
                    "X-RateLimit-Reset": str(int(retry_after)),
                },
            )
    await session.commit()
    update_request_log_context(
        api_key_id=str(principal.key_id),
        rate_tier=principal.rate_tier,
    )
    return principal


def _retry_after_seconds(window_kind: str, now: datetime) -> int:
    if window_kind == "minute":
        return 60 - now.second
    if window_kind == "hour":
        return 3600 - now.minute * 60 - now.second
    if window_kind == "day":
        return 86400 - now.hour * 3600 - now.minute * 60 - now.second
    return 60


# ---------------------------------------------------------------------
# Audit log middleware (D18).
# ---------------------------------------------------------------------


async def research_audit_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Starlette middleware: writes audit log row after the request.

    Only audits paths that mounted this middleware (the research
    router). Skips other API surfaces transparently.
    """
    if "/v1/research" not in request.url.path:
        return await call_next(request)

    ctx = RequestContext(endpoint=f"{request.method} {request.url.path}")
    request.state.research_ctx = ctx

    with bind_request_log_context(
        request_id=ctx.request_id,
        endpoint=ctx.endpoint,
        api_key_id=None,  # bound later by enforce_rate_limit if auth succeeds
    ):
        logger.debug(
            "research_request_received",
            client_ip=request.client.host if request.client else None,
        )

        response = await call_next(request)
        response.headers.setdefault("X-Request-Id", str(ctx.request_id))

        duration = time.monotonic() - ctx.started_monotonic
        _REQ_TOTAL.labels(
            endpoint=request.url.path,
            status=str(response.status_code),
            rate_tier=ctx.rate_tier,
        ).inc()
        _REQ_DURATION.labels(endpoint=request.url.path).observe(duration)

        # Successful-or-client-error completion at INFO; 5xx already
        # logged by the unhandled-exception handler at ERROR.
        update_request_log_context(
            api_key_id=str(ctx.api_key_id) if ctx.api_key_id else None,
            rate_tier=ctx.rate_tier,
            rows_returned=ctx.rows_returned,
        )
        if response.status_code < 500:
            logger.info(
                "research_request_completed",
                status_code=response.status_code,
                latency_ms=int(duration * 1000),
                rows_returned=ctx.rows_returned,
            )

        # Skip audit-log persistence on a transient failure but never
        # fail the user's request because of it. Per D18.
        try:
            engine = request.app.state.engine
            async with engine.connect() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO aslan_core.api_query_audit "
                        "(audit_id, requested_at, api_key_id, request_ip, request_id, "
                        " endpoint, query_params_sha256, as_of_requested, as_of_resolved, "
                        " rows_returned, latency_ms, status_code, error_code, "
                        " feature_flags_active) "
                        "VALUES (gen_random_uuid(), now(), :api_key_id, :ip, :req_id, "
                        " :ep, :qhash, :as_of_req, :as_of_res, :rows, :ms, :sc, "
                        " :err, :flags)"
                    ),
                    {
                        "api_key_id": str(ctx.api_key_id) if ctx.api_key_id else None,
                        "ip": request.client.host if request.client else None,
                        "req_id": str(ctx.request_id),
                        "ep": ctx.endpoint,
                        "qhash": _query_params_sha256(request),
                        "as_of_req": ctx.as_of_requested,
                        "as_of_res": ctx.as_of_resolved or datetime.now(tz=UTC),
                        "rows": ctx.rows_returned,
                        "ms": int(duration * 1000),
                        "sc": response.status_code,
                        "err": _error_code_from_status(response.status_code),
                        "flags": ctx.feature_flags_active,
                    },
                )
                await conn.commit()
            _AUDIT_TOTAL.labels(endpoint=request.url.path).inc()
        except Exception:
            # Full traceback to logs (and Sentry, if configured); a
            # missing audit row is a compliance signal worth chasing
            # but must never propagate to the client.
            logger.exception(
                "research_audit_log_failed",
                status_code=response.status_code,
                latency_ms=int(duration * 1000),
            )
        if ctx.pii_unredacted_used:
            _PII_ACCESS.labels(
                api_key_id=str(ctx.api_key_id) if ctx.api_key_id else "",
                endpoint=request.url.path,
            ).inc()
            logger.warning(
                "research_pii_access",
                api_key_id=str(ctx.api_key_id) if ctx.api_key_id else None,
            )
        return response


def _query_params_sha256(request: Request) -> bytes:
    """Per D18: hash of canonical params; raw params are PII-risky."""
    canonical = json.dumps(
        sorted(request.query_params.multi_items()), default=str
    ).encode("utf-8")
    return hashlib.sha256(canonical).digest()


def _error_code_from_status(sc: int) -> str | None:
    if sc < 400:
        return None
    mapping = {
        400: "BITEMPORAL_AS_OF_NAIVE",
        401: "AUTH_INVALID",
        403: "AUTH_FORBIDDEN",
        404: "BITEMPORAL_PRE_BITEMPORAL_REGION",
        409: "IDENTIFIER_AMBIGUOUS",
        413: "QUERY_TOO_LARGE",
        429: "RATE_LIMITED",
        503: "FEATURE_DISABLED",
    }
    return mapping.get(sc, "INTERNAL_ERROR")


__all__ = [
    "RequestContext",
    "decode_cursor",
    "encode_cursor",
    "enforce_rate_limit",
    "filters_hash",
    "get_request_context",
    "research_audit_middleware",
    "set_cache_headers",
]
