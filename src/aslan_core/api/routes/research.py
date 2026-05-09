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
) -> str:
    """Assemble a parameterized SELECT.

    Pass exactly one of ``pit_call`` (e.g. ``"ts.observation_at(:as_of)"``)
    or ``from_clause`` (e.g. ``"ref.entity e"``).

    ``where_parts`` MUST come from a fixed set of literal SQL fragments
    selected by ``if X is not None:`` branches in the caller — never
    interpolated user input. User-bound values are passed via
    SQLAlchemy ``text()`` parameter binding only.
    """
    if (pit_call is None) == (from_clause is None):
        raise ValueError("pass exactly one of pit_call / from_clause")
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


@router.get("/observations", tags=["research"])
async def observations(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
    series_id: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1: ``ts.observation_at(p_as_of)``.

    D6 rate-limit, D7 cursor pagination, D17 cache headers, D18
    audit log, D25 metrics are wired via ``enforce_rate_limit`` /
    ``ctx`` / ``set_cache_headers`` / ``research_audit_middleware``.
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.feature_flags_active = _active_flags(flags)

    where_parts: list[str] = []
    params: dict[str, Any] = {"as_of": resolved, "limit": limit}
    if series_id is not None:
        where_parts.append("series_id = :series_id")
        params["series_id"] = series_id

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat(),
        "series_id": series_id,
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

    sql = _build_filtered_sql(
        select_clause="SELECT series_id, ts, as_of, value, value_text",
        pit_call="ts.observation_at(:as_of)",
        where_parts=where_parts,
        order_by="ORDER BY series_id, ts DESC",
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
        feature_flags_active=_active_flags(flags),
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    set_cache_headers(response, as_of_resolved=resolved, body_bytes=body_bytes)
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
    restatement_basis: str | None = Query(
        default=None,
        pattern="^(as_reported|cpi_normalized)$",
        description="Optional filter; defaults to both bases.",
    ),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D8: ``ts.canonical_financial_at(p_as_of)``.

    Returns rows for the requested entity at the requested ``as_of``.
    Both ``as_reported`` and ``cpi_normalized`` bases are returned
    unless filtered. D6/D7/D17/D18/D25 wired.
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.feature_flags_active = _active_flags(flags)

    where_parts: list[str] = ["entity_id = :entity_id"]
    params: dict[str, Any] = {
        "as_of": resolved,
        "entity_id": str(entity_id),
        "limit": limit,
    }
    if restatement_basis is not None:
        where_parts.append("restatement_basis = :rb")
        params["rb"] = restatement_basis

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat(),
        "entity_id": str(entity_id),
        "restatement_basis": restatement_basis,
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

    sql = _build_filtered_sql(
        select_clause=(
            "SELECT entity_id, canonical_code, period_end, period_type, "
            "       consolidation, currency_code, accounting_standard, "
            "       restatement_basis, value, as_of, cpi_base_date, "
            "       measuring_unit_date, mapping_version"
        ),
        pit_call="ts.canonical_financial_at(:as_of)",
        where_parts=where_parts,
        order_by="ORDER BY period_end DESC, canonical_code, restatement_basis",
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
        feature_flags_active=_active_flags(flags),
        warnings=warnings,
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    set_cache_headers(response, as_of_resolved=resolved, body_bytes=body_bytes)
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
    entity_id: UUID | None = Query(default=None),
    period_end_from: datetime | None = Query(default=None),
    period_end_to: datetime | None = Query(default=None),
    restatement_basis: str | None = Query(
        default=None,
        pattern="^(nominal|as_reported|restated|adjusted|cpi_normalized)$",
    ),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D8: ``ts.financial_line_item_at(p_as_of)``.

    D6/D7/D17/D18/D25 wired via the cross-cutting observability module.
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.feature_flags_active = _active_flags(flags)

    where_parts: list[str] = []
    params: dict[str, Any] = {"as_of": resolved, "limit": limit}
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
        "as_of": resolved.isoformat(),
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
        order_by="ORDER BY period_end DESC, line_code, restatement_basis",
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
        feature_flags_active=_active_flags(flags),
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    set_cache_headers(response, as_of_resolved=resolved, body_bytes=body_bytes)
    return env


@router.get("/entities", tags=["research"])
async def entities(
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D10: list entities + lineage events.

    Phase 1 design defers the bitemporal ``ref.entity`` upgrade
    (per HANDOFF.md). v1 reads ``ref.entity`` directly (current
    state) and joins ``ref.entity_lineage_at(:as_of)`` to surface
    merge / split history. D6/D7/D17/D18/D25 wired.
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.feature_flags_active = _active_flags(flags)

    where_parts: list[str] = []
    params: dict[str, Any] = {"as_of": resolved, "limit": limit}
    if kind:
        where_parts.append("e.entity_type = :kind")
        params["kind"] = kind

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat(),
        "kind": kind,
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

    sql = _build_filtered_sql(
        select_clause=(
            "SELECT e.entity_id, e.legal_name AS name, "
            "       e.entity_type AS kind, e.created_at"
        ),
        from_clause="ref.entity e",
        where_parts=where_parts,
        order_by="ORDER BY e.created_at DESC",
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
        lineage_rows = (
            await session.execute(
                text(lineage_sql),
                {"as_of": resolved, "entity_id": str(r.entity_id)},
            )
        ).all()
        data.append(
            {
                "entity_id": str(r.entity_id),
                "name": r.name,
                "kind": r.kind,
                "created_at": r.created_at.isoformat() if r.created_at else None,
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
        feature_flags_active=_active_flags(flags),
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    set_cache_headers(response, as_of_resolved=resolved, body_bytes=body_bytes)
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
                "SELECT entity_id, legal_name AS name, "
                "       entity_type AS kind, created_at "
                "FROM ref.entity WHERE entity_id = :entity_id"
            ),
            {"entity_id": str(entity_id)},
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

    data = {
        "entity_id": str(row.entity_id),
        "name": row.name,
        "kind": row.kind,
        "created_at": row.created_at.isoformat() if row.created_at else None,
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
        "This endpoint reads a Class E+G / Class F table that has not yet "
        "received its bitemporal upgrade migration (0051 follow-up). PIT "
        "semantics are approximated using the row's published_at / "
        "received_at column until that migration lands."
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
    entity_id: UUID | None = Query(default=None),
    published_after: datetime | None = Query(default=None),
    published_before: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D3, D11: list ``kap.disclosures``.

    ``kap.disclosures`` is currently Class E+G (mutable, pre-bitemporal).
    Phase 2 will land migration 0051 to upgrade it to bitemporal; until
    then this endpoint queries the table directly and uses
    ``published_at`` as the as_of proxy (envelope warning
    ``PRE_BITEMPORAL_TABLE``). D6/D7/D17/D18/D25 wired.
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.feature_flags_active = _active_flags(flags)

    where_parts: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if entity_id is not None:
        where_parts.append("entity_id = :entity_id")
        params["entity_id"] = str(entity_id)
    if published_after is not None:
        where_parts.append("published_at >= :published_after")
        params["published_after"] = published_after
    if published_before is not None:
        where_parts.append("published_at <= :published_before")
        params["published_before"] = published_before

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat(),
        "entity_id": str(entity_id) if entity_id else None,
        "published_after": (
            published_after.isoformat() if published_after else None
        ),
        "published_before": (
            published_before.isoformat() if published_before else None
        ),
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

    sql = _build_filtered_sql(
        select_clause=(
            "SELECT disclosure_id, entity_id, title, category_code, "
            "       subcategory_code, published_at, body_fetched, "
            "       body_fetched_at, republished_as"
        ),
        pit_call="kap.disclosures",
        where_parts=where_parts,
        order_by="ORDER BY published_at DESC",
    )
    rows = await session.execute(text(sql), params)
    data = [
        {
            "disclosure_id": (
                str(r.disclosure_id) if r.disclosure_id else None
            ),
            "entity_id": str(r.entity_id) if r.entity_id else None,
            "title": r.title,
            "category_code": r.category_code,
            "subcategory_code": r.subcategory_code,
            "published_at": (
                r.published_at.isoformat() if r.published_at else None
            ),
            "body_fetched": bool(r.body_fetched) if r.body_fetched is not None else None,
            "body_fetched_at": (
                r.body_fetched_at.isoformat() if r.body_fetched_at else None
            ),
            "republished_as": (
                str(r.republished_as) if r.republished_as else None
            ),
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
        feature_flags_active=_active_flags(flags),
        warnings=[_PRE_BITEMPORAL_WARNING],
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    set_cache_headers(response, as_of_resolved=resolved, body_bytes=body_bytes)
    return env


@router.get("/disclosures/{disclosure_id}", tags=["research", "disclosures"])
async def get_disclosure(
    disclosure_id: UUID,
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D11: single disclosure (PRE_BITEMPORAL_TABLE).

    D6/D17/D18/D25 wired (single resource: no cursor pagination).
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
                "SELECT disclosure_id, entity_id, title, category_code, "
                "       subcategory_code, published_at, body_fetched, "
                "       body_fetched_at, republished_as "
                "FROM kap.disclosures "
                "WHERE disclosure_id = :disclosure_id"
            ),
            {"disclosure_id": str(disclosure_id)},
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
        "entity_id": str(row.entity_id) if row.entity_id else None,
        "title": row.title,
        "category_code": row.category_code,
        "subcategory_code": row.subcategory_code,
        "published_at": (
            row.published_at.isoformat() if row.published_at else None
        ),
        "body_fetched": (
            bool(row.body_fetched) if row.body_fetched is not None else None
        ),
        "body_fetched_at": (
            row.body_fetched_at.isoformat() if row.body_fetched_at else None
        ),
        "republished_as": (
            str(row.republished_as) if row.republished_as else None
        ),
    }
    ctx.rows_returned = 1
    env = build_envelope(
        data=data,
        request_id=ctx.request_id,
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
        warnings=[_PRE_BITEMPORAL_WARNING],
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
    entity_id: UUID | None = Query(default=None),
    received_after: datetime | None = Query(default=None),
    received_before: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1: list ``doc.filing`` (Class F, no as_of column).

    ``doc.filing`` does not yet carry an ``as_of`` column; the API
    returns the current-state row with a ``PRE_BITEMPORAL_TABLE``
    warning until migration 0051 lands. D6/D7/D17/D18/D25 wired.
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.feature_flags_active = _active_flags(flags)

    where_parts: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if entity_id is not None:
        where_parts.append("entity_id = :entity_id")
        params["entity_id"] = str(entity_id)
    if received_after is not None:
        where_parts.append("received_at >= :received_after")
        params["received_after"] = received_after
    if received_before is not None:
        where_parts.append("received_at <= :received_before")
        params["received_before"] = received_before

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat(),
        "entity_id": str(entity_id) if entity_id else None,
        "received_after": (
            received_after.isoformat() if received_after else None
        ),
        "received_before": (
            received_before.isoformat() if received_before else None
        ),
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

    sql = _build_filtered_sql(
        select_clause="SELECT *",
        pit_call="doc.filing",
        where_parts=where_parts,
        order_by="ORDER BY received_at DESC NULLS LAST",
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
            "received_at": last.get("received_at"),
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
        feature_flags_active=_active_flags(flags),
        warnings=[_PRE_BITEMPORAL_WARNING],
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    set_cache_headers(response, as_of_resolved=resolved, body_bytes=body_bytes)
    return env


@router.get("/events", tags=["research", "events"])
async def events(
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    event_type: str | None = Query(default=None),
    event_after: datetime | None = Query(default=None),
    event_before: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D30: ``agg.filing_event_at(p_as_of)``.

    Per D30 PII handling: ``payload`` may carry counterparty names;
    redact unless the principal carries ``pii_unredacted=true``. The
    envelope ``lineage`` block is populated from the primary
    ``(model_version, prompt_version, input_text_sha256)`` of the
    first row. D6/D7/D17/D18/D25 wired; ``ctx.pii_unredacted_used``
    flips when redaction is bypassed so the audit middleware can
    record the access.
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.feature_flags_active = _active_flags(flags)
    if principal is not None and principal.pii_unredacted:
        ctx.pii_unredacted_used = True

    where_parts: list[str] = []
    params: dict[str, Any] = {"as_of": resolved, "limit": limit}
    if entity_id is not None:
        where_parts.append("entity_id = :entity_id")
        params["entity_id"] = str(entity_id)
    if event_type is not None:
        where_parts.append("event_type = :event_type")
        params["event_type"] = event_type
    if event_after is not None:
        where_parts.append("event_ts >= :event_after")
        params["event_after"] = event_after
    if event_before is not None:
        where_parts.append("event_ts <= :event_before")
        params["event_before"] = event_before

    filters_for_cursor: dict[str, Any] = {
        "as_of": resolved.isoformat(),
        "entity_id": str(entity_id) if entity_id else None,
        "event_type": event_type,
        "event_after": event_after.isoformat() if event_after else None,
        "event_before": event_before.isoformat() if event_before else None,
    }
    if cursor is not None:
        decode_cursor(cursor, filters_for_cursor)
        # TODO(D7): apply keyset WHERE from decoded["anchor"] in v1.0.0-beta.

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
        order_by="ORDER BY event_ts DESC, event_seq",
    )
    rows = (await session.execute(text(sql), params)).all()
    data = [
        {
            "filing_event_id": (
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
            "event_ts": r.event_ts.isoformat() if r.event_ts else None,
            "effective_dt": (
                r.effective_dt.isoformat() if r.effective_dt else None
            ),
            "as_of": r.as_of.isoformat() if r.as_of else None,
            "payload": _redact_event_payload(r.payload, principal),
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
        lineage=lineage,
        feature_flags_active=_active_flags(flags),
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    set_cache_headers(response, as_of_resolved=resolved, body_bytes=body_bytes)
    return env


@router.get("/quality-scores", tags=["research"])
async def quality_scores(
    response: Response,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(enforce_rate_limit),
    flags: FeatureFlagState = Depends(require_master_flag),
    ctx: RequestContext = Depends(get_request_context),
    as_of: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    period_end_from: datetime | None = Query(default=None),
    period_end_to: datetime | None = Query(default=None),
    restatement_basis: str | None = Query(
        default=None,
        pattern="^(nominal|as_reported|restated|adjusted|cpi_normalized)$",
    ),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1: ``ts.entity_quality_score_at(p_as_of)``.

    D6/D7/D17/D18/D25 wired.
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()
    ctx.api_key_id = principal.key_id if principal else None
    ctx.rate_tier = principal.rate_tier if principal else "anonymous"
    ctx.as_of_requested = requested
    ctx.as_of_resolved = resolved
    ctx.feature_flags_active = _active_flags(flags)

    where_parts: list[str] = []
    params: dict[str, Any] = {"as_of": resolved, "limit": limit}
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
        "as_of": resolved.isoformat(),
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

    sql = _build_filtered_sql(
        select_clause="SELECT *",
        pit_call="ts.entity_quality_score_at(:as_of)",
        where_parts=where_parts,
        order_by="ORDER BY period_end DESC",
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
        feature_flags_active=_active_flags(flags),
        pagination=pagination,
    )
    body_bytes = _json.dumps(env, sort_keys=True, default=str).encode("utf-8")
    set_cache_headers(response, as_of_resolved=resolved, body_bytes=body_bytes)
    return env


__all__ = ["router"]
