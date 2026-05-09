"""Bitemporal Research API — main FastAPI router (Phase 3 skeleton).

Per ``docs/specs/bitemporal-research-api/SCOPE.md`` D1, D2, D4, D20,
D26.

Mounts at ``/v1/research`` in the existing ``aslan-dashboard-api``
process behind the master flag ``BITEMPORAL_API_ENABLED``.

This is a Phase 3 skeleton: each endpoint is wired to the
corresponding PIT SQL function from migration 0049/0050, returns a
SCOPE.md D15-shaped envelope, and respects the feature-flag gating
via :func:`require_master_flag`. Complete v1 capabilities (rate
limiting, audit log writes, full pagination cursor handling, PII
redaction, ETag/Cache-Control wiring) are filled in by Phase 3b–3g
follow-up commits.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.api.deps import get_session
from aslan_core.api.research_auth import (
    ApiKeyPrincipal,
    FeatureFlagState,
    get_principal,
    require_master_flag,
)
from aslan_core.api.research_envelope import (
    Pagination,
    Warning,
    build_envelope,
    now_utc,
)

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
    if dt > now_utc().replace(microsecond=999999) + _CLOCK_SKEW:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "https://docs.aslanterminal.com/errors/BITEMPORAL_AS_OF_FUTURE",
                "title": "as_of must not exceed wall-clock now()+60s",
                "code": "BITEMPORAL_AS_OF_FUTURE",
            },
        )
    return dt.astimezone(timezone.utc)


_CLOCK_SKEW = (datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
               - datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))


def _active_flags(flags: FeatureFlagState) -> list[str]:
    """Snapshot of currently-true flags for the envelope metadata."""
    return sorted(name for name, value in flags.enabled.items() if value)


# ---------------------------------------------------------------------
# Public endpoints (no auth)
# ---------------------------------------------------------------------


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Basic liveness check."""
    return {"status": "ok"}


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
) -> dict[str, Any]:
    """Per SCOPE.md D20.

    Returns ``200`` with green status when the canary's last cycle
    passed, ``503`` otherwise. The canary itself runs out-of-band
    (see ``scripts/canary_moat_2.py``) and writes its result to
    ``aslan_core.feature_flags`` under the synthetic flag
    ``MOAT_2_CANARY_STATUS`` (``true`` = green, ``false`` = red).

    Phase 3 skeleton: returns a structural placeholder until the
    canary persistence layer is wired in Phase 4f.
    """
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
        # Canary not yet wired (Phase 4f) — be honest about it.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "moat_2": "unknown",
            "reason": "canary not yet deployed (Phase 4f pending)",
            "last_run_at": None,
        }
    if row.value_bool is False:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "moat_2": "red",
            "last_run_at": row.updated_at.isoformat() if row.updated_at else None,
            "notes": row.notes,
        }
    return {
        "moat_2": "green",
        "last_run_at": row.updated_at.isoformat() if row.updated_at else None,
        "notes": row.notes,
    }


# ---------------------------------------------------------------------
# Authenticated PIT endpoints
# ---------------------------------------------------------------------


@router.get("/observations", tags=["research"])
async def observations(
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(get_principal),
    flags: FeatureFlagState = Depends(require_master_flag),
    as_of: str | None = Query(default=None),
    series_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1: ``ts.observation_at(p_as_of)``."""
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()

    query = """
        SELECT series_id, ts, as_of, value, value_text
        FROM ts.observation_at(:as_of)
        {filter}
        ORDER BY series_id, ts DESC
        LIMIT :limit
    """
    where = "WHERE series_id = :series_id" if series_id is not None else ""
    rows = await session.execute(
        text(query.format(filter=where)),
        {"as_of": resolved, "series_id": series_id, "limit": limit},
    )
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

    return build_envelope(
        data=data,
        request_id=uuid4(),
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
        pagination=Pagination(has_more=(len(data) == limit)),
    )


@router.get("/identifiers/resolve", tags=["identifiers"])
async def identifiers_resolve(
    request: Request,
    namespace: str = Query(...),
    value: str = Query(...),
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(get_principal),
    flags: FeatureFlagState = Depends(require_master_flag),
    as_of: str | None = Query(default=None),
) -> dict[str, Any]:
    """Per SCOPE.md D9: resolve (namespace, value, as_of) → entity."""
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()

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
    return build_envelope(
        data=data,
        request_id=uuid4(),
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
    )


@router.get("/financials/canonical", tags=["research"])
async def financials_canonical(
    entity_id: UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(get_principal),
    flags: FeatureFlagState = Depends(require_master_flag),
    as_of: str | None = Query(default=None),
    restatement_basis: str | None = Query(
        default=None,
        regex="^(as_reported|cpi_normalized)$",
        description="Optional filter; defaults to both bases.",
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D8: ``ts.canonical_financial_at(p_as_of)``.

    Returns rows for the requested entity at the requested ``as_of``.
    Both ``as_reported`` and ``cpi_normalized`` bases are returned
    unless filtered.
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()

    sql = (
        "SELECT entity_id, canonical_code, period_end, period_type, "
        "       consolidation, currency_code, accounting_standard, "
        "       restatement_basis, value, as_of, cpi_base_date, "
        "       measuring_unit_date, mapping_version "
        "FROM ts.canonical_financial_at(:as_of) "
        "WHERE entity_id = :entity_id "
        "{rb_filter} "
        "ORDER BY period_end DESC, canonical_code, restatement_basis "
        "LIMIT :limit"
    )
    rb_filter = "AND restatement_basis = :rb" if restatement_basis else ""
    rows = await session.execute(
        text(sql.format(rb_filter=rb_filter)),
        {
            "as_of": resolved,
            "entity_id": str(entity_id),
            "rb": restatement_basis,
            "limit": limit,
        },
    )
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

    return build_envelope(
        data=data,
        request_id=uuid4(),
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
        warnings=warnings,
        pagination=Pagination(has_more=(len(data) == limit)),
    )


# ---------------------------------------------------------------------
# Phase 3 skeleton — additional endpoints stub.
# Per SCOPE.md D1, the remaining endpoints (financials/line-items,
# entities, disclosures, filings, events, quality-scores) follow
# the same pattern. Implemented as the API graduates from Phase 3a
# skeleton to Phase 3b complete in subsequent commits. Their OpenAPI
# contracts are already defined in OPENAPI.yaml.
# ---------------------------------------------------------------------


__all__ = ["router"]
