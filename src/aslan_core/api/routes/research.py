"""Bitemporal Research API - main FastAPI router (Phase 3 skeleton).

Per ``docs/specs/bitemporal-research-api/SCOPE.md`` D1, D2, D4, D20,
D26.

Mounts at ``/v1/research`` in the existing ``aslan-dashboard-api``
process behind the master flag ``BITEMPORAL_API_ENABLED``.

This is a Phase 3 skeleton: each endpoint is wired to the
corresponding PIT SQL function from migration 0049/0050, returns a
SCOPE.md D15-shaped envelope, and respects the feature-flag gating
via :func:`require_master_flag`. Complete v1 capabilities (rate
limiting, audit log writes, full pagination cursor handling, PII
redaction, ETag/Cache-Control wiring) are filled in by Phase 3b-3g
follow-up commits.
"""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.api.deps import get_session
from aslan_core.api.research_auth import (
    ApiKeyPrincipal,
    FeatureFlagState,
    require_master_flag,
)
from aslan_core.api.research_envelope import (
    Lineage,
    Pagination,
    Warning,  # noqa: A004 - domain term shadows builtin Warning intentionally
    build_envelope,
    now_utc,
)
from aslan_core.api.research_observability import (
    RequestContext,
    decode_cursor,
    encode_cursor,
    enforce_rate_limit,
    get_request_context,
    set_cache_headers,
)

_CLOCK_SKEW_TOLERANCE = timedelta(seconds=60)
# Per SCOPE.md D19: interval-mode width hard-cap (per-key override TODO).
_AS_OF_RANGE_MAX_WIDTH = timedelta(days=5 * 365)
_LIMIT_HARD_CAP = 500

router = APIRouter(prefix="/v1/research", tags=["research"])


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _parse_as_of(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 ``as_of`` query param and reject naive strings.

    Per SCOPE.md D13: naive datetimes raise BITEMPORAL_AS_OF_NAIVE.
    """
    if raw is None:
        return None
    try:
        # fromisoformat accepts trailing 'Z' as offset on Python 3.11+.
        cleaned = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "https://docs.aslanterminal.com/errors/BITEMPORAL_AS_OF_NAIVE",
                "title": "as_of must be ISO-8601 with timezone",
                "code": "BITEMPORAL_AS_OF_NAIVE",
            },
        ) from exc
    if dt.tzinfo is None:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "https://docs.aslanterminal.com/errors/BITEMPORAL_AS_OF_NAIVE",
                "title": "as_of has no timezone offset; UTC required",
                "code": "BITEMPORAL_AS_OF_NAIVE",
            },
        )
    if dt > now_utc().replace(microsecond=999999) + _CLOCK_SKEW_TOLERANCE:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "https://docs.aslanterminal.com/errors/BITEMPORAL_AS_OF_FUTURE",
                "title": "as_of must not exceed wall-clock now()+60s",
                "code": "BITEMPORAL_AS_OF_FUTURE",
            },
        )
    return dt.astimezone(UTC)


def _parse_as_of_range(raw: str | None) -> tuple[datetime, datetime] | None:
    """Parse an interval ``as_of_range`` query param.

    Per SCOPE.md D2 / OPENAPI.yaml ``AsOfRange``: only the canonical
    half-open form ``[T1,T2)`` is accepted (T1-inclusive, T2-exclusive).
    T1 and T2 are ISO-8601 timestamps with explicit timezone.

    Earlier rounds tolerated ``(T1,T2)``, ``[T1,T2]``, and ``(T1,T2]``
    via microsecond normalization; pass-2 finding 9 flagged that as
    contract drift (the OpenAPI ``AsOfRange.pattern`` is
    ``^\\[[^,]+,[^)]+\\)$``). Generated SDKs reject any non-canonical
    form, so we now reject server-side too — one shape, one contract.

    Raises ``HTTPException(400, BITEMPORAL_INTERVAL_INVALID)`` on
    malformed input or T1 >= T2.
    """
    if raw is None:
        return None
    s = raw.strip()
    if len(s) < 5 or s[0] != "[" or s[-1] != ")":
        raise HTTPException(
            status_code=400,
            detail={
                "type": (
                    "https://docs.aslanterminal.com/errors/"
                    "BITEMPORAL_INTERVAL_INVALID"
                ),
                "title": "as_of_range must be the canonical half-open form [T1,T2)",
                "code": "BITEMPORAL_INTERVAL_INVALID",
                "extensions": {"received_value": raw},
            },
        )
    inner = s[1:-1]
    # Split on the first comma not inside a date offset (offset never
    # contains commas, so a plain split on the first ``,`` is safe).
    if "," not in inner:
        raise HTTPException(
            status_code=400,
            detail={
                "type": (
                    "https://docs.aslanterminal.com/errors/"
                    "BITEMPORAL_INTERVAL_INVALID"
                ),
                "title": "as_of_range missing comma separator",
                "code": "BITEMPORAL_INTERVAL_INVALID",
                "extensions": {"received_value": raw},
            },
        )
    t1_raw, _, t2_raw = inner.partition(",")
    try:
        t1 = datetime.fromisoformat(t1_raw.strip().replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(t2_raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "type": (
                    "https://docs.aslanterminal.com/errors/"
                    "BITEMPORAL_INTERVAL_INVALID"
                ),
                "title": "as_of_range endpoints not ISO-8601",
                "code": "BITEMPORAL_INTERVAL_INVALID",
                "extensions": {"received_value": raw},
            },
        ) from exc
    if t1.tzinfo is None or t2.tzinfo is None:
        raise HTTPException(
            status_code=400,
            detail={
                "type": (
                    "https://docs.aslanterminal.com/errors/"
                    "BITEMPORAL_INTERVAL_INVALID"
                ),
                "title": "as_of_range endpoints must carry an explicit timezone",
                "code": "BITEMPORAL_INTERVAL_INVALID",
                "extensions": {"received_value": raw},
            },
        )
    t1 = t1.astimezone(UTC)
    t2 = t2.astimezone(UTC)
    if not (t1 < t2):
        raise HTTPException(
            status_code=400,
            detail={
                "type": (
                    "https://docs.aslanterminal.com/errors/"
                    "BITEMPORAL_INTERVAL_INVALID"
                ),
                "title": "as_of_range has T1 >= T2",
                "code": "BITEMPORAL_INTERVAL_INVALID",
                "extensions": {"received_value": raw},
            },
        )
    return (t1, t2)


def _enforce_query_cost(
    *,
    as_of_range: tuple[datetime, datetime] | None,
    limit: int,
) -> None:
    """Per SCOPE.md D19: row / window caps with the documented contract.

    Raises ``HTTPException(413, QUERY_TOO_LARGE)`` when:

    - ``as_of_range`` width exceeds 5 years (per-key override deferred);
    - ``limit`` exceeds ``_LIMIT_HARD_CAP`` (500).

    Note: ``Query(default=50, ge=1)`` deliberately omits ``le=500`` so
    that an oversize page returns the documented 413 ``QUERY_TOO_LARGE``
    envelope rather than FastAPI's 422 validation shape. This was
    flagged by Codex pass-2 finding 5 / 8 / TC-074.
    """
    if as_of_range is not None:
        width = as_of_range[1] - as_of_range[0]
        if width > _AS_OF_RANGE_MAX_WIDTH:
            raise HTTPException(
                status_code=413,
                detail={
                    "type": (
                        "https://docs.aslanterminal.com/errors/QUERY_TOO_LARGE"
                    ),
                    "title": "as_of_range exceeds 5-year cap",
                    "code": "QUERY_TOO_LARGE",
                    "extensions": {
                        "width_seconds": int(width.total_seconds()),
                        "max_width_seconds": int(
                            _AS_OF_RANGE_MAX_WIDTH.total_seconds()
                        ),
                    },
                },
            )
    if limit > _LIMIT_HARD_CAP:
        raise HTTPException(
            status_code=413,
            detail={
                "type": (
                    "https://docs.aslanterminal.com/errors/QUERY_TOO_LARGE"
                ),
                "title": f"limit exceeds hard cap of {_LIMIT_HARD_CAP}",
                "code": "QUERY_TOO_LARGE",
                "extensions": {"limit": limit, "max_limit": _LIMIT_HARD_CAP},
            },
        )


def _reject_pit_with_interval(
    *, as_of: str | None, as_of_range: str | None
) -> None:
    """Per SCOPE.md D2: ``as_of`` and ``as_of_range`` are mutually exclusive."""
    if as_of is not None and as_of_range is not None:
        raise HTTPException(
            status_code=400,
            detail={
                "type": (
                    "https://docs.aslanterminal.com/errors/"
                    "BITEMPORAL_INTERVAL_INVALID"
                ),
                "title": "as_of and as_of_range are mutually exclusive",
                "code": "BITEMPORAL_INTERVAL_INVALID",
            },
        )


def _gate_interval_flag(
    *,
    as_of_range: tuple[datetime, datetime] | None,
    flags: FeatureFlagState,
) -> None:
    """Per SCOPE.md D2 / TESTPLAN TC-074: interval-mode requires the flag.

    ``BITEMPORAL_API_INTERVAL_QUERIES`` defaults to ``true`` per
    SCOPE.md §3 / migration 0048, so production behavior is unchanged
    unless an operator deliberately flips the flag off. When disabled,
    interval requests are rejected with 503 ``FEATURE_DISABLED`` so PIT
    reads remain available without restart.
    """
    if as_of_range is None:
        return
    if not flags.enabled.get("BITEMPORAL_API_INTERVAL_QUERIES", False):
        raise HTTPException(
            status_code=503,
            detail={
                "type": (
                    "https://docs.aslanterminal.com/errors/FEATURE_DISABLED"
                ),
                "title": "Interval queries are disabled",
                "code": "FEATURE_DISABLED",
                "extensions": {"flag": "BITEMPORAL_API_INTERVAL_QUERIES"},
            },
        )


# Mapping from the PIT call shape used in PIT mode to the underlying
# physical table for interval-mode queries. Per D2: interval mode
# returns the full version chain, so it must scan the SCD-4 history
# table directly (the PIT function collapses to latest-per-key via
# DISTINCT ON).
_PIT_TO_PHYSICAL_TABLE: dict[str, str] = {
    "ts.observation_at(:as_of)": "ts.observation",
    "ts.financial_line_item_at(:as_of)": "ts.financial_line_item",
    "ts.canonical_financial_at(:as_of)": "ts.canonical_financial",
    "agg.filing_event_at(:as_of)": "agg.filing_event",
    "ts.entity_quality_score_at(:as_of)": "ts.entity_quality_score",
    "ref.entity_at(:as_of)": "ref.entity_version",
    "kap.disclosures_at(:as_of)": "kap.disclosures_version",
    # ref.identifier_at uses the daterange contract (valid_from / valid_to);
    # caller branches on this entry explicitly because the WHERE shape
    # is not ``as_of >= :lo AND as_of < :hi``.
    "ref.identifier_at(:as_of)": "ref.identifier",
}


def _physical_table_for(pit_call: str) -> str:
    """Resolve the physical SCD-4 history table for an interval query.

    Raises ``KeyError`` (caller bug) if the PIT call has no mapping.
    """
    return _PIT_TO_PHYSICAL_TABLE[pit_call]


def _active_flags(flags: FeatureFlagState) -> list[str]:
    """Snapshot of currently-true flags for the envelope metadata."""
    return sorted(name for name, value in flags.enabled.items() if value)


def _build_filtered_sql(
    *,
    select_clause: str,
    pit_call: str | None = None,
    from_clause: str | None = None,
    where_parts: list[str],
    order_by: str,
    limit_param: str = ":limit",
    as_of_range: tuple[datetime, datetime] | None = None,
) -> str:
    """Assemble a parameterized SELECT.

    Pass exactly one of ``pit_call`` (e.g. ``"ts.observation_at(:as_of)"``)
    or ``from_clause`` (e.g. ``"ref.entity e"``).

    When ``as_of_range`` is supplied, the helper rewrites a ``pit_call``
    source to its underlying physical SCD-4 history table (via
    :data:`_PIT_TO_PHYSICAL_TABLE`) and appends an
    ``as_of >= :as_of_lower AND as_of < :as_of_upper`` predicate so the
    full version chain is returned, not just the latest per key.
    The caller is responsible for binding ``as_of_lower`` / ``as_of_upper``.

    ``where_parts`` MUST come from a fixed set of literal SQL fragments
    selected by ``if X is not None:`` branches in the caller — never
    interpolated user input. User-bound values are passed via
    SQLAlchemy ``text()`` parameter binding only.
    """
    if (pit_call is None) == (from_clause is None):
        raise ValueError("pass exactly one of pit_call / from_clause")
    if as_of_range is not None and pit_call is not None:
        # Interval mode: scan the physical history table directly so
        # the version chain (every row whose as_of falls in the
        # half-open interval) is returned.
        source: str | None = _physical_table_for(pit_call)
        where_parts = [*where_parts, "as_of >= :as_of_lower AND as_of < :as_of_upper"]
    else:
        source = pit_call if pit_call is not None else from_clause
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    return (
        f"{select_clause} FROM {source} {where_clause} {order_by} LIMIT {limit_param}"
    )


# ---------------------------------------------------------------------
# Public endpoints (no auth)
# ---------------------------------------------------------------------


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Basic liveness check."""
    return {"status": "ok"}


@router.get("/openapi.json", include_in_schema=False)
async def research_openapi_json(request: Request) -> dict[str, Any]:
    """Serve the auto-generated OpenAPI schema at the research-API path.

    Per SCOPE.md D21 / OPENAPI.yaml the documented contract URL is
    ``/v1/research/openapi.json``. FastAPI's default `/openapi.json`
    is also still served at the app root for backward-compat. Both
    paths return the same `app.openapi()` output.
    """
    return dict(request.app.openapi())


@router.get("/version")
async def version(
    flags: FeatureFlagState = Depends(require_master_flag),
) -> dict[str, Any]:
    """Server version + active feature flag snapshot."""
    return {
        "version": "1.0.0-alpha",
        "feature_flags_active": _active_flags(flags),
        "served_at": now_utc().isoformat(),
    }


@router.get("/verify/moat-2", tags=["verification"])
async def verify_moat_2(
    response: Response,
    session: AsyncSession = Depends(get_session),
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    """Per SCOPE.md D20 (canary status); D17 cache headers; D18 audit log.

    Returns ``200`` with green status when the canary's last cycle
    passed, ``503`` otherwise. The canary itself runs out-of-band
    (see ``scripts/canary_moat_2.py``) and writes its result to
    ``aslan_core.feature_flags`` under the synthetic flag
    ``MOAT_2_CANARY_STATUS`` (``true`` = green, ``false`` = red).

    Public endpoint: no rate-limit (per D6); audit-log row is written
    by ``research_audit_middleware`` against ``ctx``. Cache-Control is
    forced to ``no-cache`` with an ETag derived from the canary
    payload (D17): the freshness window is the whole point of this
    endpoint, so we never let a stale "green" be served.

    Phase 3 skeleton: returns a structural placeholder until the
    canary persistence layer is wired in Phase 4f.
    """
    resolved = now_utc()
    ctx.as_of_resolved = resolved

    row = (
        await session.execute(
            text(
                "SELECT value_bool, updated_at, notes "
                "FROM aslan_core.feature_flags "
                "WHERE flag_name = 'MOAT_2_CANARY_STATUS'"
            )
        )
    ).first()
    if row is None:
        # Canary not yet wired (Phase 4f) - be honest about it.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        body: dict[str, Any] = {
            "moat_2": "unknown",
            "reason": "canary not yet deployed (Phase 4f pending)",
            "last_run_at": None,
        }
    elif row.value_bool is False:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        body = {
            "moat_2": "red",
            "last_run_at": row.updated_at.isoformat() if row.updated_at else None,
            "notes": row.notes,
        }
    else:
        body = {
            "moat_2": "green",
            "last_run_at": row.updated_at.isoformat() if row.updated_at else None,
            "notes": row.notes,
        }
    ctx.rows_returned = 1 if row is not None else 0
    body_bytes = _json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    set_cache_headers(response, as_of_resolved=resolved, body_bytes=body_bytes)
    # Force no-cache for the canary status irrespective of as_of age.
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return body


# ---------------------------------------------------------------------
# Authenticated PIT endpoints
# ---------------------------------------------------------------------


async def _resolve_series_code(
    session: AsyncSession, code: str
) -> int | None:
    """Resolve a documented string ``series_code`` to its numeric ``series_id``.

    Per OPENAPI.yaml ``Observation.series_id`` ('BIST.GARAN.close') and
    README quickstart: clients pass codes; the DB uses BIGINT FKs.
    Returns None if no match exists.
    """
    row = (
        await session.execute(
            text(
                "SELECT series_id FROM ts.series_catalog "
                "WHERE series_code = :code"
            ),
            {"code": code},
        )
    ).first()
    if row is None:
        return None
    return int(row.series_id)


@router.get("/observations", tags=["research"])
async def observations(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
    as_of_range: str | None = Query(default=None),
    series_id: int | None = Query(default=None),
    series_code: str | None = Query(
        default=None,
        description="Catalog code (e.g. BIST.GARAN.close) resolved to series_id.",
    ),
    ts_from: datetime | None = Query(default=None),
    ts_to: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1),
) -> dict[str, Any]:
    """Per SCOPE.md D1: ``ts.observation_at(p_as_of)``.

    D2 interval mode (``as_of_range``), D6 rate-limit, D7 cursor
    pagination, D17 cache headers, D18 audit log, D25 metrics are
    wired via ``enforce_rate_limit`` / ``ctx`` / ``set_cache_headers``
    / ``research_audit_middleware``.
    """
    _reject_pit_with_interval(as_of=as_of, as_of_range=as_of_range)
    interval = _parse_as_of_range(as_of_range)
    _gate_interval_flag(as_of_range=interval, flags=flags)
    _enforce_query_cost(as_of_range=interval, limit=limit)
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.as_of_range = interval
    ctx.feature_flags_active = _active_flags(flags)

    # Resolve series_code -> numeric id at the API boundary (D-finding).
    if series_code is not None and series_id is None:
        resolved_id = await _resolve_series_code(session, series_code)
        if resolved_id is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "type": (
                        "https://docs.aslanterminal.com/errors/"
                        "BITEMPORAL_INTERVAL_INVALID"
                    ),
                    "title": "unknown series code",
                    "code": "BITEMPORAL_INTERVAL_INVALID",
                    "extensions": {"series_code": series_code},
                },
            )
        series_id = resolved_id

    where_parts: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if interval is None:
        params["as_of"] = resolved
    else:
        params["as_of_lower"] = interval[0]
        params["as_of_upper"] = interval[1]
    if series_id is not None:
        where_parts.append("series_id = :series_id")
        params["series_id"] = series_id
    if ts_from is not None:
        where_parts.append("ts >= :ts_from")
        params["ts_from"] = ts_from
    if ts_to is not None:
        where_parts.append("ts <= :ts_to")
        params["ts_to"] = ts_to

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat() if interval is None else None,
        "as_of_range": (
            [interval[0].isoformat(), interval[1].isoformat()]
            if interval
            else None
        ),
        "series_id": series_id,
        "ts_from": ts_from.isoformat() if ts_from else None,
        "ts_to": ts_to.isoformat() if ts_to else None,
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

    # Per TC-014: interval mode returns the version chain ordered by
    # natural key + as_of ASC; PIT mode keeps the display ordering.
    if interval is not None:
        order_by = "ORDER BY series_id, ts, as_of ASC"
    else:
        order_by = "ORDER BY series_id, ts DESC"
    sql = _build_filtered_sql(
        select_clause="SELECT series_id, ts, as_of, value, value_text",
        pit_call="ts.observation_at(:as_of)",
        where_parts=where_parts,
        order_by=order_by,
        as_of_range=interval,
    )
    rows = await session.execute(text(sql), params)
    data = [
        {
            "series_id": r.series_id,
            "ts": r.ts.isoformat() if r.ts else None,
            "as_of": r.as_of.isoformat() if r.as_of else None,
            "value": float(r.value) if r.value is not None else None,
            "value_text": r.value_text,
        }
        for r in rows
    ]
    ctx.rows_returned = len(data)

    next_cursor: str | None = None
    if len(data) == limit and data:
        last = data[-1]
        anchor = {"series_id": last["series_id"], "ts": last["ts"]}
        next_cursor = encode_cursor(
            as_of=resolved, anchor=anchor, filters=filters_for_cursor
        )
    pagination = Pagination(
        next_cursor=next_cursor, has_more=(next_cursor is not None)
    )

    env = build_envelope(
        data=data,
        request_id=ctx.request_id,
        as_of_requested=requested,
        as_of_resolved=resolved,
        as_of_range=interval,
        feature_flags_active=_active_flags(flags),
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    # Per SCOPE.md D17: interval requests are immutable when T2 <= NOW()-5m;
    # cache freshness hinges on the interval upper bound, not on now().
    cache_as_of = interval[1] if interval is not None else resolved
    set_cache_headers(response, as_of_resolved=cache_as_of, body_bytes=body_bytes)
    return env


@router.get("/identifiers/resolve", tags=["identifiers"])
async def identifiers_resolve(
    request: Request,
    response: Response,
    namespace: str = Query(...),
    value: str = Query(...),
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
) -> dict[str, Any]:
    """Per SCOPE.md D9: resolve (namespace, value, as_of) -> entity.

    Single-row resolver; D6 rate-limit, D17 cache, D18 audit, D25
    metrics applied.
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.feature_flags_active = _active_flags(flags)

    rows = (
        await session.execute(
            text(
                "SELECT entity_id, namespace, value, valid_from, valid_to, "
                "       is_primary, source_id "
                "FROM ref.identifier_at(:as_of) "
                "WHERE namespace = :ns AND value = :val"
            ),
            {"as_of": resolved, "ns": namespace, "val": value},
        )
    ).all()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail={
                "type": "https://docs.aslanterminal.com/errors/BITEMPORAL_PRE_BITEMPORAL_REGION",
                "title": "No identifier resolves at this as_of",
                "code": "BITEMPORAL_PRE_BITEMPORAL_REGION",
            },
        )
    if len(rows) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "type": "https://docs.aslanterminal.com/errors/IDENTIFIER_AMBIGUOUS",
                "title": "Multiple identifiers match",
                "code": "IDENTIFIER_AMBIGUOUS",
                "extensions": {"match_count": len(rows)},
            },
        )

    r = rows[0]
    data = {
        "entity_id": str(r.entity_id),
        "namespace": r.namespace,
        "value": r.value,
        "valid_from": r.valid_from.isoformat(),
        "valid_to": r.valid_to.isoformat(),
        "is_primary": bool(r.is_primary),
        "source_id": r.source_id,
    }
    ctx.rows_returned = 1
    env = build_envelope(
        data=data,
        request_id=ctx.request_id,
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    set_cache_headers(response, as_of_resolved=resolved, body_bytes=body_bytes)
    return env


@router.get("/financials/canonical", tags=["research"])
async def financials_canonical(
    response: Response,
    entity_id: UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
    as_of_range: str | None = Query(default=None),
    restatement_basis: str | None = Query(
        default=None,
        pattern="^(as_reported|cpi_normalized)$",
        description="Optional filter; defaults to both bases.",
    ),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D8: ``ts.canonical_financial_at(p_as_of)``.

    Returns rows for the requested entity at the requested ``as_of``.
    Both ``as_reported`` and ``cpi_normalized`` bases are returned
    unless filtered. D2 interval / D6 / D7 / D17 / D18 / D25 wired.
    """
    _reject_pit_with_interval(as_of=as_of, as_of_range=as_of_range)
    interval = _parse_as_of_range(as_of_range)
    _gate_interval_flag(as_of_range=interval, flags=flags)
    _enforce_query_cost(as_of_range=interval, limit=limit)
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.as_of_range = interval
    ctx.feature_flags_active = _active_flags(flags)

    where_parts: list[str] = ["entity_id = :entity_id"]
    params: dict[str, Any] = {
        "entity_id": str(entity_id),
        "limit": limit,
    }
    if interval is None:
        params["as_of"] = resolved
    else:
        params["as_of_lower"] = interval[0]
        params["as_of_upper"] = interval[1]
    if restatement_basis is not None:
        where_parts.append("restatement_basis = :rb")
        params["rb"] = restatement_basis

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat() if interval is None else None,
        "as_of_range": (
            [interval[0].isoformat(), interval[1].isoformat()]
            if interval
            else None
        ),
        "entity_id": str(entity_id),
        "restatement_basis": restatement_basis,
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

    # Per TC-014: interval mode returns the version chain ordered by
    # natural key + as_of ASC.
    if interval is not None:
        order_by = (
            "ORDER BY entity_id, canonical_code, period_end, period_type, "
            "consolidation, currency_code, accounting_standard, "
            "restatement_basis, cpi_base_date, mapping_version, as_of ASC"
        )
    else:
        order_by = "ORDER BY period_end DESC, canonical_code, restatement_basis"
    sql = _build_filtered_sql(
        select_clause=(
            "SELECT entity_id, canonical_code, period_end, period_type, "
            "       consolidation, currency_code, accounting_standard, "
            "       restatement_basis, value, as_of, cpi_base_date, "
            "       measuring_unit_date, mapping_version"
        ),
        pit_call="ts.canonical_financial_at(:as_of)",
        where_parts=where_parts,
        order_by=order_by,
        as_of_range=interval,
    )
    rows = await session.execute(text(sql), params)
    data = [
        {
            "entity_id": str(r.entity_id),
            "canonical_code": r.canonical_code,
            "period_end": r.period_end.isoformat(),
            "period_type": r.period_type,
            "consolidation": r.consolidation,
            "currency_code": r.currency_code,
            "accounting_standard": r.accounting_standard,
            "restatement_basis": r.restatement_basis,
            "value": float(r.value) if r.value is not None else None,
            "as_of": r.as_of.isoformat(),
            "cpi_base_date": r.cpi_base_date.isoformat() if r.cpi_base_date else None,
            "measuring_unit_date": (
                r.measuring_unit_date.isoformat() if r.measuring_unit_date else None
            ),
            "mapping_version": r.mapping_version,
        }
        for r in rows
    ]
    ctx.rows_returned = len(data)

    warnings: list[Warning] = []
    if any(d["restatement_basis"] == "cpi_normalized" for d in data):
        warnings.append(
            Warning(
                code="TAS29_PRESENT",
                message=(
                    "Response contains TAS 29 (cpi_normalized) restated rows. "
                    "Both as_reported and cpi_normalized bases are returned by "
                    "default; pass restatement_basis to filter."
                ),
                rows_affected=sum(
                    1 for d in data if d["restatement_basis"] == "cpi_normalized"
                ),
            )
        )

    next_cursor: str | None = None
    if len(data) == limit and data:
        last = data[-1]
        anchor = {
            "period_end": last["period_end"],
            "canonical_code": last["canonical_code"],
            "restatement_basis": last["restatement_basis"],
        }
        next_cursor = encode_cursor(
            as_of=resolved, anchor=anchor, filters=filters_for_cursor
        )
    pagination = Pagination(
        next_cursor=next_cursor, has_more=(next_cursor is not None)
    )

    env = build_envelope(
        data=data,
        request_id=ctx.request_id,
        as_of_requested=requested,
        as_of_resolved=resolved,
        as_of_range=interval,
        feature_flags_active=_active_flags(flags),
        warnings=warnings,
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    # Per SCOPE.md D17: interval cache freshness hinges on T2.
    cache_as_of = interval[1] if interval is not None else resolved
    set_cache_headers(response, as_of_resolved=cache_as_of, body_bytes=body_bytes)
    return env


# ---------------------------------------------------------------------
# Helpers - PII redaction (D30)
# ---------------------------------------------------------------------


_PII_KEYS = frozenset(
    {"counterparty_name", "counterparty_address", "signatory_name"}
)


def _redact_event_payload(
    payload: Any, principal: ApiKeyPrincipal | None
) -> Any:
    """Redact PII keys in an ``agg.filing_event.payload`` JSONB blob.

    Per SCOPE.md D30: counterparty / signatory fields are redacted with
    ``<REDACTED:counterparty>`` unless the principal carries
    ``pii_unredacted=true``. When pii_unredacted is true we leave the
    field intact and (TODO) increment the PII access metric.
    """
    if payload is None or not isinstance(payload, dict):
        return payload
    if principal is not None and principal.pii_unredacted:
        # TODO(observability): pii_access_total{key_id=...}
        return payload
    redacted = dict(payload)
    for key in list(redacted.keys()):
        if key in _PII_KEYS:
            redacted[key] = "<REDACTED:counterparty>"
    return redacted


# ---------------------------------------------------------------------
# Authenticated PIT endpoints - financials/line-items, entities,
# disclosures, filings, events, quality-scores. Per SCOPE.md D1.
# ---------------------------------------------------------------------


@router.get("/financials/line-items", tags=["research"])
async def financials_line_items(
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
    as_of_range: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    period_end_from: datetime | None = Query(default=None),
    period_end_to: datetime | None = Query(default=None),
    restatement_basis: str | None = Query(
        default=None,
        pattern="^(nominal|as_reported|restated|adjusted|cpi_normalized)$",
    ),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D8: ``ts.financial_line_item_at(p_as_of)``.

    D2 interval, D6/D7/D17/D18/D25 wired via the cross-cutting
    observability module.
    """
    _reject_pit_with_interval(as_of=as_of, as_of_range=as_of_range)
    interval = _parse_as_of_range(as_of_range)
    _gate_interval_flag(as_of_range=interval, flags=flags)
    _enforce_query_cost(as_of_range=interval, limit=limit)
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.as_of_range = interval
    ctx.feature_flags_active = _active_flags(flags)

    where_parts: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if interval is None:
        params["as_of"] = resolved
    else:
        params["as_of_lower"] = interval[0]
        params["as_of_upper"] = interval[1]
    if entity_id is not None:
        where_parts.append("entity_id = :entity_id")
        params["entity_id"] = str(entity_id)
    if period_end_from is not None:
        where_parts.append("period_end >= :period_end_from")
        params["period_end_from"] = period_end_from
    if period_end_to is not None:
        where_parts.append("period_end <= :period_end_to")
        params["period_end_to"] = period_end_to
    if restatement_basis is not None:
        where_parts.append("restatement_basis = :restatement_basis")
        params["restatement_basis"] = restatement_basis

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat() if interval is None else None,
        "as_of_range": (
            [interval[0].isoformat(), interval[1].isoformat()]
            if interval
            else None
        ),
        "entity_id": str(entity_id) if entity_id else None,
        "period_end_from": (
            period_end_from.isoformat() if period_end_from else None
        ),
        "period_end_to": (
            period_end_to.isoformat() if period_end_to else None
        ),
        "restatement_basis": restatement_basis,
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

    # Per TC-014: interval mode returns the version chain ordered by
    # natural key + as_of ASC.
    if interval is not None:
        order_by = (
            "ORDER BY entity_id, filing_id, statement_type, line_code, "
            "consolidation, period_end, as_of ASC"
        )
    else:
        order_by = "ORDER BY period_end DESC, line_code, restatement_basis"
    sql = _build_filtered_sql(
        select_clause=(
            "SELECT entity_id, filing_id, statement_type, line_code, "
            "       parent_line_code, period_start, period_end, period_type, "
            "       value, currency_code, consolidation, accounting_standard, "
            "       restatement_basis, as_of, ingestion_run_id, "
            "       measuring_unit_date, derivation_reason"
        ),
        pit_call="ts.financial_line_item_at(:as_of)",
        where_parts=where_parts,
        order_by=order_by,
        as_of_range=interval,
    )
    rows = await session.execute(text(sql), params)
    data = [
        {
            "entity_id": str(r.entity_id) if r.entity_id else None,
            "filing_id": str(r.filing_id) if r.filing_id else None,
            "statement_type": r.statement_type,
            "line_code": r.line_code,
            "parent_line_code": r.parent_line_code,
            "period_start": r.period_start.isoformat() if r.period_start else None,
            "period_end": r.period_end.isoformat() if r.period_end else None,
            "period_type": r.period_type,
            "value": float(r.value) if r.value is not None else None,
            "currency_code": r.currency_code,
            "consolidation": r.consolidation,
            "accounting_standard": r.accounting_standard,
            "restatement_basis": r.restatement_basis,
            "as_of": r.as_of.isoformat() if r.as_of else None,
            "ingestion_run_id": (
                str(r.ingestion_run_id) if r.ingestion_run_id else None
            ),
            "measuring_unit_date": (
                r.measuring_unit_date.isoformat() if r.measuring_unit_date else None
            ),
            "derivation_reason": r.derivation_reason,
        }
        for r in rows
    ]
    ctx.rows_returned = len(data)

    next_cursor: str | None = None
    if len(data) == limit and data:
        last = data[-1]
        anchor = {
            "period_end": last["period_end"],
            "line_code": last["line_code"],
            "restatement_basis": last["restatement_basis"],
        }
        next_cursor = encode_cursor(
            as_of=resolved, anchor=anchor, filters=filters_for_cursor
        )
    pagination = Pagination(
        next_cursor=next_cursor, has_more=(next_cursor is not None)
    )

    env = build_envelope(
        data=data,
        request_id=ctx.request_id,
        as_of_requested=requested,
        as_of_resolved=resolved,
        as_of_range=interval,
        feature_flags_active=_active_flags(flags),
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    # Per SCOPE.md D17: interval cache freshness hinges on T2.
    cache_as_of = interval[1] if interval is not None else resolved
    set_cache_headers(response, as_of_resolved=cache_as_of, body_bytes=body_bytes)
    return env


@router.get("/entities", tags=["research"])
async def entities(
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
    as_of_range: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D10: list entities + lineage events.

    Reads via ``ref.entity_at(:as_of)`` (migration 0046, SCD-4
    bitemporal upgrade) so historical ``as_of`` returns the entity
    state at that time, not the present-day row. Interval mode (D2)
    queries ``ref.entity_version`` directly to return the version
    chain. D6/D7/D17/D18/D25 wired.
    """
    _reject_pit_with_interval(as_of=as_of, as_of_range=as_of_range)
    interval = _parse_as_of_range(as_of_range)
    _gate_interval_flag(as_of_range=interval, flags=flags)
    _enforce_query_cost(as_of_range=interval, limit=limit)
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.as_of_range = interval
    ctx.feature_flags_active = _active_flags(flags)

    where_parts: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if interval is None:
        params["as_of"] = resolved
    else:
        params["as_of_lower"] = interval[0]
        params["as_of_upper"] = interval[1]
    if kind:
        where_parts.append("entity_type = :kind")
        params["kind"] = kind

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat() if interval is None else None,
        "as_of_range": (
            [interval[0].isoformat(), interval[1].isoformat()]
            if interval
            else None
        ),
        "kind": kind,
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

    # Per TC-014: interval mode returns the version chain ordered by
    # natural key + as_of ASC.
    if interval is not None:
        order_by = "ORDER BY entity_id, as_of ASC"
    else:
        order_by = "ORDER BY legal_name, entity_id"
    sql = _build_filtered_sql(
        select_clause=(
            "SELECT entity_id, as_of, event_kind, entity_type, legal_name, "
            "       country_code, merged_from_entity_ids"
        ),
        pit_call="ref.entity_at(:as_of)",
        where_parts=where_parts,
        order_by=order_by,
        as_of_range=interval,
    )
    rows = (await session.execute(text(sql), params)).all()

    lineage_sql = (
        "SELECT event_kind, from_entity_id, into_entity_id, "
        "       event_at, reason "
        "FROM ref.entity_lineage_at(:as_of) "
        "WHERE from_entity_id = :entity_id OR into_entity_id = :entity_id "
        "ORDER BY event_at"
    )

    data: list[dict[str, Any]] = []
    for r in rows:
        # Per SCOPE.md D2 / pass-2 finding 7: in interval mode, each
        # version row's lineage must be evaluated at that row's own
        # as_of, not at the request's resolved (NOW()) timestamp,
        # otherwise a historical version row carries future merge/split
        # events. Loop-and-rebind is fine for ref.entity-scale data; a
        # bulk join is a v1.0.0-beta perf follow-up.
        lineage_as_of = r.as_of if interval is not None else resolved
        lineage_rows = (
            await session.execute(
                text(lineage_sql),
                {"as_of": lineage_as_of, "entity_id": str(r.entity_id)},
            )
        ).all()
        merged_from = r.merged_from_entity_ids
        if merged_from is None:
            merged_from_list: list[str] = []
        elif isinstance(merged_from, list):
            merged_from_list = [str(m) for m in merged_from]
        else:
            merged_from_list = [str(merged_from)]
        # Derive split_from_entity_id from a 'split' lineage event if any.
        split_from: str | None = None
        for le in lineage_rows:
            if le.event_kind == "split" and le.into_entity_id == r.entity_id:
                split_from = (
                    str(le.from_entity_id) if le.from_entity_id else None
                )
                break
        data.append(
            {
                "entity_id": str(r.entity_id),
                "canonical_name": r.legal_name,
                "kind": r.entity_type,
                "country": r.country_code,
                "merged_from_entity_ids": merged_from_list,
                "split_from_entity_id": split_from,
                "as_of": r.as_of.isoformat() if r.as_of else None,
                "lineage_events": [
                    {
                        "event_kind": le.event_kind,
                        "from": (
                            str(le.from_entity_id) if le.from_entity_id else None
                        ),
                        "into": (
                            str(le.into_entity_id) if le.into_entity_id else None
                        ),
                        "event_at": (
                            le.event_at.isoformat() if le.event_at else None
                        ),
                        "reason": le.reason,
                    }
                    for le in lineage_rows
                ],
            }
        )
    ctx.rows_returned = len(data)

    next_cursor: str | None = None
    if len(data) == limit and data:
        last = data[-1]
        anchor = {"entity_id": last["entity_id"]}
        next_cursor = encode_cursor(
            as_of=resolved, anchor=anchor, filters=filters_for_cursor
        )
    pagination = Pagination(
        next_cursor=next_cursor, has_more=(next_cursor is not None)
    )

    env = build_envelope(
        data=data,
        request_id=ctx.request_id,
        as_of_requested=requested,
        as_of_resolved=resolved,
        as_of_range=interval,
        feature_flags_active=_active_flags(flags),
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    # Per SCOPE.md D17: interval cache freshness hinges on T2.
    cache_as_of = interval[1] if interval is not None else resolved
    set_cache_headers(response, as_of_resolved=cache_as_of, body_bytes=body_bytes)
    return env


@router.get("/entities/{entity_id}", tags=["research"])
async def get_entity(
    entity_id: UUID,
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D10: single entity + lineage events at ``as_of``.

    D6/D17/D18/D25 wired (single-resource: no cursor pagination).
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.feature_flags_active = _active_flags(flags)

    row = (
        await session.execute(
            text(
                "SELECT entity_id, as_of, event_kind, entity_type, legal_name, "
                "       country_code, merged_from_entity_ids "
                "FROM ref.entity_at(:as_of) "
                "WHERE entity_id = :entity_id"
            ),
            {"as_of": resolved, "entity_id": str(entity_id)},
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "type": (
                    "https://docs.aslanterminal.com/errors/"
                    "BITEMPORAL_PRE_BITEMPORAL_REGION"
                ),
                "title": "Entity not found at this as_of",
                "code": "BITEMPORAL_PRE_BITEMPORAL_REGION",
            },
        )

    lineage_rows = (
        await session.execute(
            text(
                "SELECT event_kind, from_entity_id, into_entity_id, "
                "       event_at, reason "
                "FROM ref.entity_lineage_at(:as_of) "
                "WHERE (from_entity_id = :entity_id "
                "       OR into_entity_id = :entity_id) "
                "  AND event_at <= :as_of "
                "ORDER BY event_at"
            ),
            {"as_of": resolved, "entity_id": str(entity_id)},
        )
    ).all()

    merged_from = row.merged_from_entity_ids
    if merged_from is None:
        merged_from_list: list[str] = []
    elif isinstance(merged_from, list):
        merged_from_list = [str(m) for m in merged_from]
    else:
        merged_from_list = [str(merged_from)]
    split_from: str | None = None
    for le in lineage_rows:
        if le.event_kind == "split" and le.into_entity_id == row.entity_id:
            split_from = str(le.from_entity_id) if le.from_entity_id else None
            break

    data = {
        "entity_id": str(row.entity_id),
        "canonical_name": row.legal_name,
        "kind": row.entity_type,
        "country": row.country_code,
        "merged_from_entity_ids": merged_from_list,
        "split_from_entity_id": split_from,
        "as_of": row.as_of.isoformat() if row.as_of else None,
        "lineage_events": [
            {
                "event_kind": le.event_kind,
                "from": str(le.from_entity_id) if le.from_entity_id else None,
                "into": str(le.into_entity_id) if le.into_entity_id else None,
                "event_at": le.event_at.isoformat() if le.event_at else None,
                "reason": le.reason,
            }
            for le in lineage_rows
        ],
    }
    ctx.rows_returned = 1
    env = build_envelope(
        data=data,
        request_id=ctx.request_id,
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    set_cache_headers(response, as_of_resolved=resolved, body_bytes=body_bytes)
    return env


_PRE_BITEMPORAL_WARNING = Warning(
    code="PRE_BITEMPORAL_TABLE",
    message=(
        "This endpoint reads a Class F table that does not yet have a PIT "
        "function in v1; rows reflect current state regardless of as_of. "
        "Adding doc.filing_at is a v1.0.0-beta follow-up."
    ),
)


@router.get("/disclosures", tags=["research", "disclosures"])
async def disclosures(
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
    as_of_range: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    kap_company_id: str | None = Query(
        default=None,
        description="Per OpenAPI: filter by KAP company id (kap.disclosures.kap_id).",
    ),
    form_type: str | None = Query(
        default=None,
        description="Per OpenAPI: filter by KAP category code (e.g. ÖDA).",
    ),
    published_from: datetime | None = Query(
        default=None,
        description="Per OpenAPI: published_at >= published_from.",
    ),
    published_to: datetime | None = Query(
        default=None,
        description="Per OpenAPI: published_at <= published_to.",
    ),
    published_after: datetime | None = Query(
        default=None,
        description="Deprecated alias for published_from (D24).",
    ),
    published_before: datetime | None = Query(
        default=None,
        description="Deprecated alias for published_to (D24).",
    ),
    include_pre_bitemporal: bool = Query(
        default=False,
        description=(
            "Per SCOPE.md D3: when true, also include rows whose "
            "as_of_provenance is 'pre_bitemporal_unknown' "
            "(as_of=NULL backfill rows excluded by kap.disclosures_at)."
        ),
    ),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D3, D11: list ``kap.disclosures``.

    Reads via ``kap.disclosures_at(:as_of)`` (migration 0051, SCD-4
    bitemporal upgrade). ``as_of_provenance`` indicates how the
    historical row was timestamped (e.g. ``index_fetched_at``,
    ``body_fetched_at``, or ``pre_bitemporal_unknown`` for backfill).
    Interval mode (D2) queries ``kap.disclosures_version`` directly to
    return the version chain. D6/D7/D17/D18/D25 wired.
    """
    _reject_pit_with_interval(as_of=as_of, as_of_range=as_of_range)
    interval = _parse_as_of_range(as_of_range)
    _gate_interval_flag(as_of_range=interval, flags=flags)
    _enforce_query_cost(as_of_range=interval, limit=limit)
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.as_of_range = interval
    ctx.feature_flags_active = _active_flags(flags)

    # Per pass-2 finding 6 / OpenAPI: the documented OpenAPI parameter
    # name takes precedence when both old (deprecated) and new aliases
    # are passed. Old aliases stay accepted to avoid breaking existing
    # clients; D24 deprecation header is a v1.0.0-beta follow-up.
    effective_published_from = (
        published_from if published_from is not None else published_after
    )
    effective_published_to = (
        published_to if published_to is not None else published_before
    )

    where_parts: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if interval is None:
        params["as_of"] = resolved
    else:
        params["as_of_lower"] = interval[0]
        params["as_of_upper"] = interval[1]
    if entity_id is not None:
        where_parts.append("entity_id = :entity_id")
        params["entity_id"] = str(entity_id)
    if kap_company_id is not None:
        where_parts.append("kap_id = :kap_company_id")
        params["kap_company_id"] = kap_company_id
    if form_type is not None:
        where_parts.append("category_code = :form_type")
        params["form_type"] = form_type
    if effective_published_from is not None:
        where_parts.append("published_at >= :published_from")
        params["published_from"] = effective_published_from
    if effective_published_to is not None:
        where_parts.append("published_at <= :published_to")
        params["published_to"] = effective_published_to

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat() if interval is None else None,
        "as_of_range": (
            [interval[0].isoformat(), interval[1].isoformat()]
            if interval
            else None
        ),
        "entity_id": str(entity_id) if entity_id else None,
        "kap_company_id": kap_company_id,
        "form_type": form_type,
        "published_from": (
            effective_published_from.isoformat()
            if effective_published_from
            else None
        ),
        "published_to": (
            effective_published_to.isoformat()
            if effective_published_to
            else None
        ),
        "include_pre_bitemporal": include_pre_bitemporal,
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

    # Per SCOPE.md D3 / pass-2 finding 2: by default we read the PIT
    # function (which filters ``v.as_of <= p_as_of`` and so excludes
    # NULL as_of pre-bitemporal backfill rows). When
    # ``include_pre_bitemporal=true`` we read ``kap.disclosures_version``
    # directly with the same predicate plus an OR for the
    # pre_bitemporal_unknown provenance, so the backfill rows surface.
    # Interval mode (already a version-table scan) is unaffected.
    if include_pre_bitemporal and interval is None:
        # Direct scan of the SCD-4 version table; mirror the PIT-function
        # latest-per-key shape via DISTINCT ON.
        from_clause_pre = (
            "(SELECT DISTINCT ON (v.disclosure_id) "
            "        v.disclosure_id, v.as_of, v.event_kind, v.as_of_provenance, "
            "        v.entity_id, v.kap_id, v.title, v.category_code, "
            "        v.subcategory_code, v.published_at, v.is_amendment, "
            "        v.body_fetched, v.body_fetched_at, v.parent_disclosure_id "
            " FROM kap.disclosures_version v "
            " WHERE (v.as_of <= :as_of "
            "        OR v.as_of_provenance = 'pre_bitemporal_unknown') "
            "   AND v.event_kind <> 'deleted' "
            " ORDER BY v.disclosure_id, v.as_of DESC NULLS LAST) AS d"
        )
        order_by = "ORDER BY published_at DESC"
        sql = _build_filtered_sql(
            select_clause=(
                "SELECT disclosure_id, as_of, event_kind, as_of_provenance, "
                "       entity_id, kap_id, title, category_code, "
                "       subcategory_code, published_at, is_amendment, "
                "       body_fetched, body_fetched_at, parent_disclosure_id"
            ),
            from_clause=from_clause_pre,
            where_parts=where_parts,
            order_by=order_by,
            as_of_range=None,
        )
    else:
        # Per TC-014: interval mode returns the version chain ordered
        # by natural key + as_of ASC; PIT mode keeps display ordering.
        if interval is not None:
            order_by = "ORDER BY disclosure_id, as_of ASC"
        else:
            order_by = "ORDER BY published_at DESC"
        sql = _build_filtered_sql(
            select_clause=(
                "SELECT disclosure_id, as_of, event_kind, as_of_provenance, "
                "       entity_id, kap_id, title, category_code, "
                "       subcategory_code, published_at, is_amendment, "
                "       body_fetched, body_fetched_at, parent_disclosure_id"
            ),
            pit_call="kap.disclosures_at(:as_of)",
            where_parts=where_parts,
            order_by=order_by,
            as_of_range=interval,
        )
    rows = await session.execute(text(sql), params)
    data = [
        {
            "disclosure_id": (
                str(r.disclosure_id) if r.disclosure_id else None
            ),
            "kap_company_id": r.kap_id,
            "entity_id": str(r.entity_id) if r.entity_id else None,
            "form_type": r.category_code,
            "title": r.title,
            "category_code": r.category_code,
            "subcategory_code": r.subcategory_code,
            "published_at": (
                r.published_at.isoformat() if r.published_at else None
            ),
            "is_amendment": (
                bool(r.is_amendment) if r.is_amendment is not None else None
            ),
            "body_fetched": (
                bool(r.body_fetched) if r.body_fetched is not None else None
            ),
            "body_fetched_at": (
                r.body_fetched_at.isoformat() if r.body_fetched_at else None
            ),
            "republished_as": (
                str(r.parent_disclosure_id) if r.parent_disclosure_id else None
            ),
            "as_of": r.as_of.isoformat() if r.as_of else None,
            "as_of_provenance": r.as_of_provenance,
            "pre_bitemporal": (
                r.as_of_provenance == "pre_bitemporal_unknown"
            ),
            "event_kind": r.event_kind,
        }
        for r in rows
    ]
    ctx.rows_returned = len(data)

    next_cursor: str | None = None
    if len(data) == limit and data:
        last = data[-1]
        anchor = {
            "published_at": last["published_at"],
            "disclosure_id": last["disclosure_id"],
        }
        next_cursor = encode_cursor(
            as_of=resolved, anchor=anchor, filters=filters_for_cursor
        )
    pagination = Pagination(
        next_cursor=next_cursor, has_more=(next_cursor is not None)
    )

    env = build_envelope(
        data=data,
        request_id=ctx.request_id,
        as_of_requested=requested,
        as_of_resolved=resolved,
        as_of_range=interval,
        feature_flags_active=_active_flags(flags),
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    # Per SCOPE.md D17: interval cache freshness hinges on T2.
    cache_as_of = interval[1] if interval is not None else resolved
    set_cache_headers(response, as_of_resolved=cache_as_of, body_bytes=body_bytes)
    return env


@router.get("/disclosures/{disclosure_id}", tags=["research", "disclosures"])
async def get_disclosure(
    disclosure_id: str,
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
    include_pre_bitemporal: bool = Query(
        default=False,
        description=(
            "Per SCOPE.md D3: when true, also accept rows whose "
            "as_of_provenance is 'pre_bitemporal_unknown' "
            "(otherwise hidden by kap.disclosures_at)."
        ),
    ),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D11: single disclosure.

    KAP `disclosure_id` is TEXT (e.g. ``KAP-2024-1234567``) per the
    crawl schema, so the path parameter is `str`, not UUID.

    D6/D17/D18/D25 wired (single resource: no cursor pagination).
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.feature_flags_active = _active_flags(flags)

    # Per SCOPE.md D3 / pass-2 finding 2: when include_pre_bitemporal
    # is true, scan kap.disclosures_version directly so the backfill
    # rows (as_of NULL, provenance 'pre_bitemporal_unknown') surface;
    # the PIT function would filter them out via ``v.as_of <= p_as_of``.
    if include_pre_bitemporal:
        row = (
            await session.execute(
                text(
                    "SELECT DISTINCT ON (v.disclosure_id) "
                    "       v.disclosure_id, v.as_of, v.event_kind, "
                    "       v.as_of_provenance, v.entity_id, v.kap_id, "
                    "       v.title, v.category_code, v.subcategory_code, "
                    "       v.published_at, v.is_amendment, v.body_fetched, "
                    "       v.body_fetched_at, v.parent_disclosure_id "
                    "FROM kap.disclosures_version v "
                    "WHERE v.disclosure_id = :disclosure_id "
                    "  AND (v.as_of <= :as_of "
                    "       OR v.as_of_provenance = 'pre_bitemporal_unknown') "
                    "  AND v.event_kind <> 'deleted' "
                    "ORDER BY v.disclosure_id, v.as_of DESC NULLS LAST"
                ),
                {"as_of": resolved, "disclosure_id": disclosure_id},
            )
        ).first()
    else:
        row = (
            await session.execute(
                text(
                    "SELECT disclosure_id, as_of, event_kind, as_of_provenance, "
                    "       entity_id, kap_id, title, category_code, "
                    "       subcategory_code, published_at, is_amendment, "
                    "       body_fetched, body_fetched_at, parent_disclosure_id "
                    "FROM kap.disclosures_at(:as_of) "
                    "WHERE disclosure_id = :disclosure_id"
                ),
                {"as_of": resolved, "disclosure_id": disclosure_id},
            )
        ).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "type": (
                    "https://docs.aslanterminal.com/errors/"
                    "BITEMPORAL_PRE_BITEMPORAL_REGION"
                ),
                "title": "Disclosure not found",
                "code": "BITEMPORAL_PRE_BITEMPORAL_REGION",
            },
        )

    data = {
        "disclosure_id": str(row.disclosure_id) if row.disclosure_id else None,
        "kap_company_id": row.kap_id,
        "entity_id": str(row.entity_id) if row.entity_id else None,
        "form_type": row.category_code,
        "title": row.title,
        "category_code": row.category_code,
        "subcategory_code": row.subcategory_code,
        "published_at": (
            row.published_at.isoformat() if row.published_at else None
        ),
        "is_amendment": (
            bool(row.is_amendment) if row.is_amendment is not None else None
        ),
        "body_fetched": (
            bool(row.body_fetched) if row.body_fetched is not None else None
        ),
        "body_fetched_at": (
            row.body_fetched_at.isoformat() if row.body_fetched_at else None
        ),
        "republished_as": (
            str(row.parent_disclosure_id) if row.parent_disclosure_id else None
        ),
        "as_of": row.as_of.isoformat() if row.as_of else None,
        "as_of_provenance": row.as_of_provenance,
        "pre_bitemporal": row.as_of_provenance == "pre_bitemporal_unknown",
        "event_kind": row.event_kind,
    }
    ctx.rows_returned = 1
    env = build_envelope(
        data=data,
        request_id=ctx.request_id,
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    set_cache_headers(response, as_of_resolved=resolved, body_bytes=body_bytes)
    return env


@router.get("/filings", tags=["research", "filings"])
async def filings(
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
    as_of_range: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    source_kind: str | None = Query(
        default=None,
        description=(
            "Per OpenAPI: filter by src.source.kind (joined via "
            "doc.filing.source_id). Enum: kap, bist, tcmb, mkk, tefas, other."
        ),
    ),
    filed_from: datetime | None = Query(
        default=None,
        description=(
            "Per OpenAPI: published_at >= filed_from "
            "(doc.filing has no received_at column; published_at is the "
            "filed timestamp)."
        ),
    ),
    filed_to: datetime | None = Query(
        default=None,
        description="Per OpenAPI: published_at <= filed_to.",
    ),
    received_after: datetime | None = Query(
        default=None,
        description="Deprecated alias for filed_from (D24).",
    ),
    received_before: datetime | None = Query(
        default=None,
        description="Deprecated alias for filed_to (D24).",
    ),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1),
) -> dict[str, Any]:
    """Per SCOPE.md D1: list ``doc.filing`` (Class F, no PIT function).

    ``doc.filing`` does not yet have a ``doc.filing_at`` PIT function in
    v1; rows reflect current state regardless of ``as_of`` /
    ``as_of_range``. The envelope carries a ``PRE_BITEMPORAL_TABLE``
    warning. Adding ``doc.filing_at`` is a v1.0.0-beta follow-up.
    D6/D7/D17/D18/D25 wired.
    """
    _reject_pit_with_interval(as_of=as_of, as_of_range=as_of_range)
    interval = _parse_as_of_range(as_of_range)
    _gate_interval_flag(as_of_range=interval, flags=flags)
    _enforce_query_cost(as_of_range=interval, limit=limit)
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.as_of_range = interval
    ctx.feature_flags_active = _active_flags(flags)

    # Per pass-2 finding 6 / OpenAPI: prefer the documented OpenAPI
    # parameter names; the legacy ``received_*`` names stay as aliases.
    # ``doc.filing`` does not store a "received_at" column — the closest
    # documented timestamp is ``published_at``, so both names map to it.
    effective_filed_from = (
        filed_from if filed_from is not None else received_after
    )
    effective_filed_to = filed_to if filed_to is not None else received_before

    where_parts: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if entity_id is not None:
        where_parts.append("f.entity_id = :entity_id")
        params["entity_id"] = str(entity_id)
    if source_kind is not None:
        # ``doc.filing`` has no source_kind column; resolve via the
        # ``src.source.kind`` lookup joined on ``source_id``.
        where_parts.append("s.kind = :source_kind")
        params["source_kind"] = source_kind
    if effective_filed_from is not None:
        where_parts.append("f.published_at >= :filed_from")
        params["filed_from"] = effective_filed_from
    if effective_filed_to is not None:
        where_parts.append("f.published_at <= :filed_to")
        params["filed_to"] = effective_filed_to

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat() if interval is None else None,
        "as_of_range": (
            [interval[0].isoformat(), interval[1].isoformat()]
            if interval
            else None
        ),
        "entity_id": str(entity_id) if entity_id else None,
        "source_kind": source_kind,
        "filed_from": (
            effective_filed_from.isoformat() if effective_filed_from else None
        ),
        "filed_to": (
            effective_filed_to.isoformat() if effective_filed_to else None
        ),
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

    # doc.filing has no PIT function and no SCD-4 version table in v1;
    # both PIT and interval modes return the current-state rows with a
    # PRE_BITEMPORAL_TABLE warning attached. We always join src.source
    # so source_kind filtering is uniform; the join is cheap (PK lookup)
    # and the source row exists for every filing by FK.
    sql = _build_filtered_sql(
        select_clause="SELECT f.*, s.kind AS source_kind",
        from_clause="doc.filing f JOIN src.source s ON s.source_id = f.source_id",
        where_parts=where_parts,
        order_by="ORDER BY f.published_at DESC NULLS LAST",
    )
    result = await session.execute(text(sql), params)
    columns = list(result.keys())
    data: list[dict[str, Any]] = []
    for row in result:
        record: dict[str, Any] = {}
        for col, val in zip(columns, row, strict=False):
            if isinstance(val, datetime):
                record[col] = val.isoformat()
            elif isinstance(val, UUID):
                record[col] = str(val)
            else:
                record[col] = val
        data.append(record)

    ctx.rows_returned = len(data)

    next_cursor: str | None = None
    if len(data) == limit and data:
        last = data[-1]
        anchor = {
            "published_at": last.get("published_at"),
            "filing_id": last.get("filing_id"),
        }
        next_cursor = encode_cursor(
            as_of=resolved, anchor=anchor, filters=filters_for_cursor
        )
    pagination = Pagination(
        next_cursor=next_cursor, has_more=(next_cursor is not None)
    )

    env = build_envelope(
        data=data,
        request_id=ctx.request_id,
        as_of_requested=requested,
        as_of_resolved=resolved,
        as_of_range=interval,
        feature_flags_active=_active_flags(flags),
        warnings=[_PRE_BITEMPORAL_WARNING],
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    # Per SCOPE.md D17: interval cache freshness hinges on T2.
    cache_as_of = interval[1] if interval is not None else resolved
    set_cache_headers(response, as_of_resolved=cache_as_of, body_bytes=body_bytes)
    return env


@router.get("/events", tags=["research", "events"])
async def events(
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
    as_of_range: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    filing_id: UUID | None = Query(
        default=None,
        description="Per OpenAPI: filter by source filing UUID.",
    ),
    event_type: str | None = Query(default=None),
    occurred_from: datetime | None = Query(
        default=None,
        description="Per OpenAPI: event_ts >= occurred_from.",
    ),
    occurred_to: datetime | None = Query(
        default=None,
        description="Per OpenAPI: event_ts <= occurred_to.",
    ),
    event_after: datetime | None = Query(
        default=None,
        description="Deprecated alias for occurred_from (D24).",
    ),
    event_before: datetime | None = Query(
        default=None,
        description="Deprecated alias for occurred_to (D24).",
    ),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D30: ``agg.filing_event_at(p_as_of)``.

    Per D30 PII handling: ``attributes`` may carry counterparty names;
    redact unless the principal carries ``pii_unredacted=true``. The
    envelope ``lineage`` block is populated from the primary
    ``(model_version, prompt_version, input_text_sha256)`` of the
    first row. D2 interval / D6 / D7 / D17 / D18 / D25 wired;
    ``ctx.pii_unredacted_used`` flips when redaction is bypassed so the
    audit middleware can record the access.

    Response shape uses OpenAPI's terms (``event_id``, ``occurred_at``,
    ``attributes``); these are renames of the underlying physical
    columns ``filing_event_id``, ``event_ts``, ``payload`` respectively.
    """
    _reject_pit_with_interval(as_of=as_of, as_of_range=as_of_range)
    interval = _parse_as_of_range(as_of_range)
    _gate_interval_flag(as_of_range=interval, flags=flags)
    _enforce_query_cost(as_of_range=interval, limit=limit)
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.as_of_range = interval
    ctx.feature_flags_active = _active_flags(flags)
    if principal is not None and principal.pii_unredacted:
        ctx.pii_unredacted_used = True

    # Per pass-2 finding 6 / OpenAPI: prefer the documented OpenAPI
    # parameter names; legacy ``event_*`` aliases keep working.
    effective_occurred_from = (
        occurred_from if occurred_from is not None else event_after
    )
    effective_occurred_to = (
        occurred_to if occurred_to is not None else event_before
    )

    where_parts: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if interval is None:
        params["as_of"] = resolved
    else:
        params["as_of_lower"] = interval[0]
        params["as_of_upper"] = interval[1]
    if entity_id is not None:
        where_parts.append("entity_id = :entity_id")
        params["entity_id"] = str(entity_id)
    if filing_id is not None:
        where_parts.append("filing_id = :filing_id")
        params["filing_id"] = str(filing_id)
    if event_type is not None:
        where_parts.append("event_type = :event_type")
        params["event_type"] = event_type
    if effective_occurred_from is not None:
        where_parts.append("event_ts >= :occurred_from")
        params["occurred_from"] = effective_occurred_from
    if effective_occurred_to is not None:
        where_parts.append("event_ts <= :occurred_to")
        params["occurred_to"] = effective_occurred_to

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat() if interval is None else None,
        "as_of_range": (
            [interval[0].isoformat(), interval[1].isoformat()]
            if interval
            else None
        ),
        "entity_id": str(entity_id) if entity_id else None,
        "filing_id": str(filing_id) if filing_id else None,
        "event_type": event_type,
        "occurred_from": (
            effective_occurred_from.isoformat()
            if effective_occurred_from
            else None
        ),
        "occurred_to": (
            effective_occurred_to.isoformat()
            if effective_occurred_to
            else None
        ),
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

    # Per TC-014: interval mode returns the version chain ordered by
    # natural key + as_of ASC.
    if interval is not None:
        order_by = "ORDER BY filing_id, event_type, event_seq, as_of ASC"
    else:
        order_by = "ORDER BY event_ts DESC, event_seq"
    sql = _build_filtered_sql(
        select_clause=(
            "SELECT filing_event_id, filing_id, source_id, source_filing_ref, "
            "       event_type, event_seq, entity_id, counterparty_entity_id, "
            "       event_ts, effective_dt, as_of, payload, "
            "       primary_confidence, final_confidence, "
            "       primary_model_version, primary_prompt_version, "
            "       input_text_sha256"
        ),
        pit_call="agg.filing_event_at(:as_of)",
        where_parts=where_parts,
        order_by=order_by,
        as_of_range=interval,
    )
    rows = (await session.execute(text(sql), params)).all()
    data = [
        {
            # OpenAPI surface name -> physical column.
            "event_id": (
                str(r.filing_event_id) if r.filing_event_id else None
            ),
            "filing_id": str(r.filing_id) if r.filing_id else None,
            "source_id": r.source_id,
            "source_filing_ref": r.source_filing_ref,
            "event_type": r.event_type,
            "event_seq": r.event_seq,
            "entity_id": str(r.entity_id) if r.entity_id else None,
            "counterparty_entity_id": (
                str(r.counterparty_entity_id) if r.counterparty_entity_id else None
            ),
            "occurred_at": r.event_ts.isoformat() if r.event_ts else None,
            "effective_dt": (
                r.effective_dt.isoformat() if r.effective_dt else None
            ),
            "as_of": r.as_of.isoformat() if r.as_of else None,
            "attributes": _redact_event_payload(r.payload, principal),
            "primary_confidence": (
                float(r.primary_confidence)
                if r.primary_confidence is not None
                else None
            ),
            "final_confidence": (
                float(r.final_confidence)
                if r.final_confidence is not None
                else None
            ),
            "primary_model_version": r.primary_model_version,
            "primary_prompt_version": r.primary_prompt_version,
        }
        for r in rows
    ]

    lineage: Lineage | None = None
    if rows:
        first = rows[0]
        lineage = Lineage(
            model_identity=first.primary_model_version,
            prompt_version=first.primary_prompt_version,
            raw_bytes_sha256=(
                first.input_text_sha256.hex()
                if isinstance(first.input_text_sha256, (bytes, bytearray))
                else first.input_text_sha256
            ),
        )

    ctx.rows_returned = len(data)

    next_cursor: str | None = None
    if len(data) == limit and data:
        last = data[-1]
        anchor = {
            "filing_id": last["filing_id"],
            "event_seq": last["event_seq"],
        }
        next_cursor = encode_cursor(
            as_of=resolved, anchor=anchor, filters=filters_for_cursor
        )
    pagination = Pagination(
        next_cursor=next_cursor, has_more=(next_cursor is not None)
    )

    env = build_envelope(
        data=data,
        request_id=ctx.request_id,
        as_of_requested=requested,
        as_of_resolved=resolved,
        as_of_range=interval,
        lineage=lineage,
        feature_flags_active=_active_flags(flags),
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    # Per SCOPE.md D17: interval cache freshness hinges on T2.
    cache_as_of = interval[1] if interval is not None else resolved
    set_cache_headers(response, as_of_resolved=cache_as_of, body_bytes=body_bytes)
    return env


@router.get("/quality-scores", tags=["research"])
async def quality_scores(
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
    as_of_range: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    period_end_from: datetime | None = Query(default=None),
    period_end_to: datetime | None = Query(default=None),
    restatement_basis: str | None = Query(
        default=None,
        pattern="^(nominal|as_reported|restated|adjusted|cpi_normalized)$",
    ),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1),
) -> dict[str, Any]:
    """Per SCOPE.md D1: ``ts.entity_quality_score_at(p_as_of)``.

    D2 interval, D6/D7/D17/D18/D25 wired.
    """
    _reject_pit_with_interval(as_of=as_of, as_of_range=as_of_range)
    interval = _parse_as_of_range(as_of_range)
    _gate_interval_flag(as_of_range=interval, flags=flags)
    _enforce_query_cost(as_of_range=interval, limit=limit)
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.as_of_range = interval
    ctx.feature_flags_active = _active_flags(flags)

    where_parts: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if interval is None:
        params["as_of"] = resolved
    else:
        params["as_of_lower"] = interval[0]
        params["as_of_upper"] = interval[1]
    if entity_id is not None:
        where_parts.append("entity_id = :entity_id")
        params["entity_id"] = str(entity_id)
    if period_end_from is not None:
        where_parts.append("period_end >= :period_end_from")
        params["period_end_from"] = period_end_from
    if period_end_to is not None:
        where_parts.append("period_end <= :period_end_to")
        params["period_end_to"] = period_end_to
    if restatement_basis is not None:
        where_parts.append("restatement_basis = :restatement_basis")
        params["restatement_basis"] = restatement_basis

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat() if interval is None else None,
        "as_of_range": (
            [interval[0].isoformat(), interval[1].isoformat()]
            if interval
            else None
        ),
        "entity_id": str(entity_id) if entity_id else None,
        "period_end_from": (
            period_end_from.isoformat() if period_end_from else None
        ),
        "period_end_to": (
            period_end_to.isoformat() if period_end_to else None
        ),
        "restatement_basis": restatement_basis,
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

    # Per TC-014: interval mode returns the version chain ordered by
    # natural key + as_of ASC.
    if interval is not None:
        order_by = (
            "ORDER BY entity_id, period_end, period_type, consolidation, "
            "currency_code, accounting_standard, restatement_basis, "
            "cpi_base_date, mapping_version, as_of ASC"
        )
    else:
        order_by = "ORDER BY period_end DESC"
    sql = _build_filtered_sql(
        select_clause="SELECT *",
        pit_call="ts.entity_quality_score_at(:as_of)",
        where_parts=where_parts,
        order_by=order_by,
        as_of_range=interval,
    )
    result = await session.execute(text(sql), params)
    columns = list(result.keys())
    data: list[dict[str, Any]] = []
    for row in result:
        record: dict[str, Any] = {}
        for col, val in zip(columns, row, strict=False):
            if isinstance(val, datetime):
                record[col] = val.isoformat()
            elif isinstance(val, UUID):
                record[col] = str(val)
            else:
                record[col] = (
                    float(val)
                    if hasattr(val, "is_finite") and not isinstance(val, bool)
                    else val
                )
        data.append(record)
    ctx.rows_returned = len(data)

    next_cursor: str | None = None
    if len(data) == limit and data:
        last = data[-1]
        anchor = {
            "period_end": last.get("period_end"),
            "entity_id": last.get("entity_id"),
        }
        next_cursor = encode_cursor(
            as_of=resolved, anchor=anchor, filters=filters_for_cursor
        )
    pagination = Pagination(
        next_cursor=next_cursor, has_more=(next_cursor is not None)
    )

    env = build_envelope(
        data=data,
        request_id=ctx.request_id,
        as_of_requested=requested,
        as_of_resolved=resolved,
        as_of_range=interval,
        feature_flags_active=_active_flags(flags),
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    set_cache_headers(response, as_of_resolved=resolved, body_bytes=body_bytes)
    return env


__all__ = ["router"]
