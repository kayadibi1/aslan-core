"""Query functions for entity listing and quality scores.

Provides:
- ``list_entities``: paginated entity listing with optional name search,
  joining aggregates from ``ts.canonical_financial`` and quality scores
  from ``ts.entity_quality_score``.
- ``get_entity_quality``: per-entity quality-score history with public
  check output (allowlisted fields only).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Boolean, String, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.query.schemas import (
    Consolidation,
    EntitySummary,
    PeriodType,
    Principal,
    QualityCheckPublic,
    QualityScorePublic,
    RestatementBasis,
)

# Allowlisted keys that may be exposed from the internal checks JSONB.
_CHECK_PUBLIC_KEYS = frozenset({"state", "delta_pct", "coverage_pct"})

_LIST_ENTITIES_SQL = text("""\
WITH cf_agg AS (
    SELECT
        entity_id,
        MAX(period_end)  AS latest_period,
        COUNT(*)::INT    AS canonical_line_count
    FROM ts.canonical_financial
    GROUP BY entity_id
),
qs_agg AS (
    SELECT
        entity_id,
        ROUND(AVG(score))::INT AS avg_quality_score
    FROM ts.entity_quality_score
    GROUP BY entity_id
)
SELECT
    e.entity_id,
    e.legal_name,
    id.value          AS ticker,
    cf.latest_period,
    qs.avg_quality_score,
    COALESCE(cf.canonical_line_count, 0) AS canonical_line_count
FROM ref.entity e
LEFT JOIN ref.identifier id
    ON id.entity_id = e.entity_id
    AND id.namespace = 'bist_ticker'
LEFT JOIN cf_agg cf
    ON cf.entity_id = e.entity_id
LEFT JOIN qs_agg qs
    ON qs.entity_id = e.entity_id
WHERE (:search IS NULL OR e.legal_name ILIKE '%' || :search || '%')
  AND (:has_financials = FALSE OR cf.entity_id IS NOT NULL)
ORDER BY e.legal_name
LIMIT :lim OFFSET :off
""").bindparams(
    bindparam("search", type_=String),
    bindparam("has_financials", type_=Boolean),
)

_LIST_ENTITIES_COUNT_SQL = text("""\
WITH cf_agg AS (
    SELECT DISTINCT entity_id
    FROM ts.canonical_financial
)
SELECT COUNT(*) AS total
FROM ref.entity e
LEFT JOIN cf_agg cf
    ON cf.entity_id = e.entity_id
WHERE (:search IS NULL OR e.legal_name ILIKE '%' || :search || '%')
  AND (:has_financials = FALSE OR cf.entity_id IS NOT NULL)
""").bindparams(
    bindparam("search", type_=String),
    bindparam("has_financials", type_=Boolean),
)

_QUALITY_SCORES_SQL = text("""\
SELECT
    period_end,
    period_type,
    consolidation,
    score,
    insufficient_data,
    checks
FROM ts.entity_quality_score
WHERE entity_id = :entity_id
  AND restatement_basis = :restatement_basis
ORDER BY period_end DESC
LIMIT :lim
""")


async def list_entities(
    session: AsyncSession,
    caller: Principal,  # reserved for future authz
    *,
    search: str | None = None,
    has_financials: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[EntitySummary], int]:
    """Return a paginated list of entities with aggregate financial metadata.

    Parameters
    ----------
    session:
        An active SQLAlchemy async session.
    caller:
        The authenticated principal (reserved for future authorisation).
    search:
        Optional case-insensitive substring match on ``legal_name``.
        Capped at 100 characters.
    has_financials:
        When *True* (the default), only entities that have at least one
        row in ``ts.canonical_financial`` are returned.
    limit:
        Page size.  Must be 1-100.
    offset:
        Row offset.  Must be 0-10 000.

    Returns
    -------
    tuple[list[EntitySummary], int]
        A ``(rows, total_count)`` pair.

    Raises
    ------
    ValueError
        If *limit* or *offset* is outside the allowed range, or *search*
        exceeds 100 characters.
    """
    if not 1 <= limit <= 100:
        msg = f"limit must be 1-100, got {limit}"
        raise ValueError(msg)
    if not 0 <= offset <= 10_000:
        msg = f"offset must be 0-10000, got {offset}"
        raise ValueError(msg)
    if search is not None and len(search) > 100:
        msg = "search string must be at most 100 characters"
        raise ValueError(msg)

    params = {
        "search": search,
        "has_financials": has_financials,
        "lim": limit,
        "off": offset,
    }

    rows = (await session.execute(_LIST_ENTITIES_SQL, params)).all()
    total = (await session.execute(_LIST_ENTITIES_COUNT_SQL, params)).scalar_one()

    summaries = [
        EntitySummary(
            entity_id=r.entity_id,
            legal_name=r.legal_name,
            ticker=r.ticker,
            latest_period=r.latest_period,
            avg_quality_score=r.avg_quality_score,
            canonical_line_count=r.canonical_line_count,
        )
        for r in rows
    ]
    return summaries, total


async def get_entity_quality(
    session: AsyncSession,
    caller: Principal,  # reserved for future authz
    entity_id: UUID,
    *,
    restatement_basis: RestatementBasis = RestatementBasis.AS_REPORTED,
    limit: int = 10,
) -> list[QualityScorePublic]:
    """Return the most recent quality scores for an entity.

    Parameters
    ----------
    session:
        An active SQLAlchemy async session.
    caller:
        The authenticated principal (reserved for future authorisation).
    entity_id:
        The entity to query.
    restatement_basis:
        Filter by restatement basis (default: ``as_reported``).
    limit:
        Maximum number of periods to return.  Must be 1-100.

    Returns
    -------
    list[QualityScorePublic]
        Quality scores ordered by ``period_end`` descending.

    Raises
    ------
    ValueError
        If *limit* is outside the allowed range.
    """
    if not 1 <= limit <= 100:
        msg = f"limit must be 1-100, got {limit}"
        raise ValueError(msg)

    rows = (
        await session.execute(
            _QUALITY_SCORES_SQL,
            {
                "entity_id": entity_id,
                "restatement_basis": str(restatement_basis),
                "lim": limit,
            },
        )
    ).all()

    results: list[QualityScorePublic] = []
    for r in rows:
        full_jsonb: dict[str, object] = r.checks or {}
        inner_checks: dict[str, object] = full_jsonb.get("checks", {})  # type: ignore[assignment]
        public_checks: dict[str, QualityCheckPublic] = {}
        for check_name, detail in inner_checks.items():
            if not isinstance(detail, dict):
                continue
            public_checks[check_name] = QualityCheckPublic(
                **{k: v for k, v in detail.items() if k in _CHECK_PUBLIC_KEYS},
            )
        results.append(
            QualityScorePublic(
                period_end=r.period_end,
                period_type=PeriodType(r.period_type),
                consolidation=Consolidation(r.consolidation),
                score=r.score,
                insufficient_data=r.insufficient_data,
                checks=public_checks,
            ),
        )
    return results


__all__ = [
    "get_entity_quality",
    "list_entities",
]
