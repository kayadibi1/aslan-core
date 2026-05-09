# ruff: noqa: S608
# (S608 disabled file-wide: every SQL string in this module is built
# from a fixed set of literal fragments selected by `if ... is not None`
# branches; user input is bound via SQLAlchemy `text()` parameters, never
# concatenated into the SQL string.)
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

from datetime import UTC, datetime
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
    Lineage,
    Pagination,
    Warning,  # noqa: A004 - domain term shadows builtin Warning intentionally
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
    return dt.astimezone(UTC)


_CLOCK_SKEW = (datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
               - datetime(2026, 1, 1, 0, 0, tzinfo=UTC))


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
        # Canary not yet wired (Phase 4f) - be honest about it.
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
    """Per SCOPE.md D9: resolve (namespace, value, as_of) -> entity."""
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
        pattern="^(as_reported|cpi_normalized)$",
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
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(get_principal),
    flags: FeatureFlagState = Depends(require_master_flag),
    as_of: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    period_end_from: datetime | None = Query(default=None),
    period_end_to: datetime | None = Query(default=None),
    restatement_basis: str | None = Query(
        default=None,
        pattern="^(nominal|as_reported|restated|adjusted|cpi_normalized)$",
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D8: ``ts.financial_line_item_at(p_as_of)``."""
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()

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
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = (
        "SELECT entity_id, filing_id, statement_type, line_code, "
        "       parent_line_code, period_start, period_end, period_type, "
        "       value, currency_code, consolidation, accounting_standard, "
        "       restatement_basis, as_of, ingestion_run_id, "
        "       measuring_unit_date, derivation_reason "
        "FROM ts.financial_line_item_at(:as_of) "
        f"{where_clause} "
        "ORDER BY period_end DESC, line_code, restatement_basis "
        "LIMIT :limit"
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
    return build_envelope(
        data=data,
        request_id=uuid4(),
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
        pagination=Pagination(has_more=(len(data) == limit)),
    )


@router.get("/entities", tags=["research"])
async def entities(
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(get_principal),
    flags: FeatureFlagState = Depends(require_master_flag),
    as_of: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D10: list entities + lineage events.

    Phase 1 design defers the bitemporal ``ref.entity`` upgrade
    (per HANDOFF.md). v1 reads ``ref.entity`` directly (current
    state) and joins ``ref.entity_lineage_at(:as_of)`` to surface
    merge / split history.
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()

    where_clause = "WHERE e.kind = :kind" if kind else ""
    params: dict[str, Any] = {"as_of": resolved, "limit": limit}
    if kind:
        params["kind"] = kind

    sql = (
        "SELECT e.entity_id, e.name, e.kind, e.created_at "
        "FROM ref.entity e "
        f"{where_clause} "
        "ORDER BY e.created_at DESC "
        "LIMIT :limit"
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

    return build_envelope(
        data=data,
        request_id=uuid4(),
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
        pagination=Pagination(has_more=(len(data) == limit)),
    )


@router.get("/entities/{entity_id}", tags=["research"])
async def get_entity(
    entity_id: UUID,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(get_principal),
    flags: FeatureFlagState = Depends(require_master_flag),
    as_of: str | None = Query(default=None),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D10: single entity + lineage events at ``as_of``."""
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()

    row = (
        await session.execute(
            text(
                "SELECT entity_id, name, kind, created_at "
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
    return build_envelope(
        data=data,
        request_id=uuid4(),
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
    )


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
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(get_principal),
    flags: FeatureFlagState = Depends(require_master_flag),
    as_of: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    published_after: datetime | None = Query(default=None),
    published_before: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D3, D11: list ``kap.disclosures``.

    ``kap.disclosures`` is currently Class E+G (mutable, pre-bitemporal).
    Phase 2 will land migration 0051 to upgrade it to bitemporal; until
    then this endpoint queries the table directly and uses
    ``published_at`` as the as_of proxy (envelope warning
    ``PRE_BITEMPORAL_TABLE``).
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()

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
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = (
        "SELECT disclosure_id, entity_id, title, category_code, "
        "       subcategory_code, published_at, body_fetched, "
        "       body_fetched_at, republished_as "
        "FROM kap.disclosures "
        f"{where_clause} "
        "ORDER BY published_at DESC "
        "LIMIT :limit"
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
    return build_envelope(
        data=data,
        request_id=uuid4(),
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
        warnings=[_PRE_BITEMPORAL_WARNING],
        pagination=Pagination(has_more=(len(data) == limit)),
    )


@router.get("/disclosures/{disclosure_id}", tags=["research", "disclosures"])
async def get_disclosure(
    disclosure_id: UUID,
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(get_principal),
    flags: FeatureFlagState = Depends(require_master_flag),
    as_of: str | None = Query(default=None),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D11: single disclosure (PRE_BITEMPORAL_TABLE)."""
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()

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
    return build_envelope(
        data=data,
        request_id=uuid4(),
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
        warnings=[_PRE_BITEMPORAL_WARNING],
    )


@router.get("/filings", tags=["research", "filings"])
async def filings(
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(get_principal),
    flags: FeatureFlagState = Depends(require_master_flag),
    as_of: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    received_after: datetime | None = Query(default=None),
    received_before: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1: list ``doc.filing`` (Class F, no as_of column).

    ``doc.filing`` does not yet carry an ``as_of`` column; the API
    returns the current-state row with a ``PRE_BITEMPORAL_TABLE``
    warning until migration 0051 lands.
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()

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
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = (
        "SELECT * FROM doc.filing "
        f"{where_clause} "
        "ORDER BY received_at DESC NULLS LAST "
        "LIMIT :limit"
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

    return build_envelope(
        data=data,
        request_id=uuid4(),
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
        warnings=[_PRE_BITEMPORAL_WARNING],
        pagination=Pagination(has_more=(len(data) == limit)),
    )


@router.get("/events", tags=["research", "events"])
async def events(
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(get_principal),
    flags: FeatureFlagState = Depends(require_master_flag),
    as_of: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    event_type: str | None = Query(default=None),
    event_after: datetime | None = Query(default=None),
    event_before: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1, D30: ``agg.filing_event_at(p_as_of)``.

    Per D30 PII handling: ``payload`` may carry counterparty names;
    redact unless the principal carries ``pii_unredacted=true``. The
    envelope ``lineage`` block is populated from the primary
    ``(model_version, prompt_version, input_text_sha256)`` of the
    first row.
    """
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()

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
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = (
        "SELECT filing_event_id, filing_id, source_id, source_filing_ref, "
        "       event_type, event_seq, entity_id, counterparty_entity_id, "
        "       event_ts, effective_dt, as_of, payload, "
        "       primary_confidence, final_confidence, "
        "       primary_model_version, primary_prompt_version, "
        "       input_text_sha256 "
        "FROM agg.filing_event_at(:as_of) "
        f"{where_clause} "
        "ORDER BY event_ts DESC, event_seq "
        "LIMIT :limit"
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

    return build_envelope(
        data=data,
        request_id=uuid4(),
        as_of_requested=requested,
        as_of_resolved=resolved,
        lineage=lineage,
        feature_flags_active=_active_flags(flags),
        pagination=Pagination(has_more=(len(data) == limit)),
    )


@router.get("/quality-scores", tags=["research"])
async def quality_scores(
    session: AsyncSession = Depends(get_session),
    principal: ApiKeyPrincipal | None = Depends(get_principal),
    flags: FeatureFlagState = Depends(require_master_flag),
    as_of: str | None = Query(default=None),
    entity_id: UUID | None = Query(default=None),
    period_end_from: datetime | None = Query(default=None),
    period_end_to: datetime | None = Query(default=None),
    restatement_basis: str | None = Query(
        default=None,
        pattern="^(nominal|as_reported|restated|adjusted|cpi_normalized)$",
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Per SCOPE.md D1: ``ts.entity_quality_score_at(p_as_of)``."""
    requested = _parse_as_of(as_of)
    resolved = requested or now_utc()

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
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = (
        "SELECT * FROM ts.entity_quality_score_at(:as_of) "
        f"{where_clause} "
        "ORDER BY period_end DESC "
        "LIMIT :limit"
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

    return build_envelope(
        data=data,
        request_id=uuid4(),
        as_of_requested=requested,
        as_of_resolved=resolved,
        feature_flags_active=_active_flags(flags),
        pagination=Pagination(has_more=(len(data) == limit)),
    )


__all__ = ["router"]
