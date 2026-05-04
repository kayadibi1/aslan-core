"""Query functions for canonical financial data.

Provides three public functions:

- ``get_entity_financials`` -- period-grouped financial lines for one entity.
- ``compare_metric`` -- single canonical code across multiple entities.
- ``get_metric_timeseries`` -- one metric for one entity, both restatement bases.

All SQL is parameterised via :bind-param style; no f-string interpolation.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.query.schemas import (
    Consolidation,
    MetricPoint,
    PeriodData,
    PeriodType,
    Principal,
    RestatementBasis,
    StatementType,
    TimeseriesPoint,
)

__all__ = [
    "compare_metric",
    "get_entity_financials",
    "get_metric_timeseries",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_COMPARE_ENTITIES = 10

# ---------------------------------------------------------------------------
# 1. get_entity_financials
# ---------------------------------------------------------------------------

_ENTITY_FINANCIALS_SQL = text("""\
SELECT DISTINCT ON (
        cf.canonical_code,
        cf.period_end,
        cf.period_type,
        cf.consolidation,
        cf.restatement_basis
    )
    cf.period_end,
    cf.period_type,
    cf.consolidation,
    cf.restatement_basis,
    cf.canonical_code,
    cf.value,
    (SELECT qs.score
     FROM ts.entity_quality_score qs
     WHERE qs.entity_id        = cf.entity_id
       AND qs.period_end        = cf.period_end
       AND qs.period_type       = cf.period_type
       AND qs.consolidation     = cf.consolidation
       AND qs.restatement_basis = cf.restatement_basis
     ORDER BY qs.mapping_version DESC
     LIMIT 1
    ) AS quality_score
FROM ts.canonical_financial cf
WHERE cf.entity_id          = :entity_id
  AND cf.period_type        = :period_type
  AND cf.restatement_basis  = :restatement_basis
  AND cf.consolidation      = :consolidation
ORDER BY
    cf.canonical_code,
    cf.period_end,
    cf.period_type,
    cf.consolidation,
    cf.restatement_basis,
    cf.mapping_version DESC
""")

_ENTITY_FINANCIALS_STMT_SQL = text("""\
SELECT DISTINCT ON (
        cf.canonical_code,
        cf.period_end,
        cf.period_type,
        cf.consolidation,
        cf.restatement_basis
    )
    cf.period_end,
    cf.period_type,
    cf.consolidation,
    cf.restatement_basis,
    cf.canonical_code,
    cf.value,
    (SELECT qs.score
     FROM ts.entity_quality_score qs
     WHERE qs.entity_id        = cf.entity_id
       AND qs.period_end        = cf.period_end
       AND qs.period_type       = cf.period_type
       AND qs.consolidation     = cf.consolidation
       AND qs.restatement_basis = cf.restatement_basis
     ORDER BY qs.mapping_version DESC
     LIMIT 1
    ) AS quality_score
FROM ts.canonical_financial cf
WHERE cf.entity_id          = :entity_id
  AND cf.period_type        = :period_type
  AND cf.restatement_basis  = :restatement_basis
  AND cf.consolidation      = :consolidation
  AND cf.canonical_code LIKE :statement_prefix
ORDER BY
    cf.canonical_code,
    cf.period_end,
    cf.period_type,
    cf.consolidation,
    cf.restatement_basis,
    cf.mapping_version DESC
""")


async def get_entity_financials(
    session: AsyncSession,
    caller: Principal,  # reserved for authz
    entity_id: UUID,
    *,
    statement_type: StatementType | None = None,
    period_type: PeriodType,
    restatement_basis: RestatementBasis,
    consolidation: Consolidation,
    limit: int = 20,
) -> list[PeriodData]:
    """Return canonical financial rows for *entity_id*, grouped by period.

    Uses ``DISTINCT ON`` to pick the latest ``mapping_version`` per
    canonical key.  Rows are grouped into :class:`PeriodData` objects
    ordered by ``period_end DESC``.
    """
    params: dict[str, object] = {
        "entity_id": entity_id,
        "period_type": period_type.value,
        "restatement_basis": restatement_basis.value,
        "consolidation": consolidation.value,
    }

    if statement_type is not None:
        stmt = _ENTITY_FINANCIALS_STMT_SQL
        params["statement_prefix"] = f"{statement_type.value}.%"
    else:
        stmt = _ENTITY_FINANCIALS_SQL

    result = await session.execute(stmt, params)
    rows = result.fetchall()

    # Group rows by (period_end, period_type, consolidation, restatement_basis)
    groups: dict[
        tuple[date, str, str, str],
        tuple[dict[str, Decimal | None], int | None],
    ] = {}

    for row in rows:
        key = (row.period_end, row.period_type, row.consolidation, row.restatement_basis)
        if key not in groups:
            groups[key] = ({}, row.quality_score)
        groups[key][0][row.canonical_code] = row.value

    periods = [
        PeriodData(
            period_end=key[0],
            period_type=PeriodType(key[1]),
            consolidation=Consolidation(key[2]),
            restatement_basis=RestatementBasis(key[3]),
            lines=lines,
            quality_score=qs,
        )
        for key, (lines, qs) in groups.items()
    ]

    # Sort by period_end DESC for a stable contract.
    periods.sort(key=lambda p: p.period_end, reverse=True)
    return periods[:limit]


# ---------------------------------------------------------------------------
# 2. compare_metric
# ---------------------------------------------------------------------------

_COMPARE_SQL = text("""\
SELECT DISTINCT ON (
        cf.entity_id,
        cf.period_end,
        cf.period_type
    )
    cf.entity_id,
    cf.period_end,
    cf.period_type,
    cf.restatement_basis,
    cf.value
FROM ts.canonical_financial cf
WHERE cf.entity_id          = ANY(:entity_ids)
  AND cf.canonical_code     = :canonical_code
  AND cf.restatement_basis  = :restatement_basis
  AND cf.period_type        = :period_type
  AND cf.consolidation      = :consolidation
ORDER BY
    cf.entity_id,
    cf.period_end,
    cf.period_type,
    cf.mapping_version DESC
""")


async def compare_metric(
    session: AsyncSession,
    caller: Principal,
    entity_ids: list[UUID],
    canonical_code: str,
    *,
    restatement_basis: RestatementBasis,
    period_type: PeriodType,
    consolidation: Consolidation,
    limit: int = 20,
) -> dict[UUID, list[MetricPoint]]:
    """Compare a single *canonical_code* across up to 10 entities.

    Raises :class:`ValueError` when *entity_ids* is empty or exceeds 10.
    Duplicates are silently removed.
    """
    unique_ids = list(dict.fromkeys(entity_ids))  # preserves order

    if len(unique_ids) < 1:
        msg = "entity_ids must contain at least one UUID"
        raise ValueError(msg)
    if len(unique_ids) > _MAX_COMPARE_ENTITIES:
        msg = f"entity_ids must contain at most {_MAX_COMPARE_ENTITIES} UUIDs"
        raise ValueError(msg)

    result = await session.execute(
        _COMPARE_SQL,
        {
            "entity_ids": [str(uid) for uid in unique_ids],
            "canonical_code": canonical_code,
            "restatement_basis": restatement_basis.value,
            "period_type": period_type.value,
            "consolidation": consolidation.value,
        },
    )
    rows = result.fetchall()

    out: dict[UUID, list[MetricPoint]] = defaultdict(list)
    for row in rows:
        out[row.entity_id].append(
            MetricPoint(
                period_end=row.period_end,
                period_type=PeriodType(row.period_type),
                value=row.value,
                restatement_basis=RestatementBasis(row.restatement_basis),
            )
        )

    # Sort each entity's points DESC + enforce limit.
    for uid in out:
        out[uid].sort(key=lambda p: p.period_end, reverse=True)
        out[uid] = out[uid][:limit]

    # Ensure every requested id appears in the result even if no data.
    for uid in unique_ids:
        if uid not in out:
            out[uid] = []

    return dict(out)


# ---------------------------------------------------------------------------
# 3. get_metric_timeseries
# ---------------------------------------------------------------------------

_TS_BOTH_SQL = text("""\
SELECT
    COALESCE(ar.period_end, cn.period_end) AS period_end,
    COALESCE(ar.period_type, cn.period_type) AS period_type,
    ar.value  AS as_reported,
    cn.value  AS cpi_normalized,
    cn.cpi_base_date
FROM (
    SELECT DISTINCT ON (period_end, period_type)
        period_end, period_type, value
    FROM ts.canonical_financial
    WHERE entity_id         = :entity_id
      AND canonical_code    = :canonical_code
      AND restatement_basis = 'as_reported'
      AND period_type       = :period_type
      AND consolidation     = :consolidation
    ORDER BY period_end, period_type, mapping_version DESC
) ar
FULL OUTER JOIN (
    SELECT DISTINCT ON (period_end, period_type)
        period_end, period_type, value, cpi_base_date
    FROM ts.canonical_financial
    WHERE entity_id         = :entity_id
      AND canonical_code    = :canonical_code
      AND restatement_basis = 'cpi_normalized'
      AND period_type       = :period_type
      AND consolidation     = :consolidation
    ORDER BY period_end, period_type, mapping_version DESC
) cn
    ON ar.period_end  = cn.period_end
   AND ar.period_type = cn.period_type
ORDER BY COALESCE(ar.period_end, cn.period_end) DESC
""")

_TS_SINGLE_SQL = text("""\
SELECT DISTINCT ON (cf.period_end, cf.period_type)
    cf.period_end,
    cf.period_type,
    cf.value,
    cf.restatement_basis,
    cf.cpi_base_date
FROM ts.canonical_financial cf
WHERE cf.entity_id         = :entity_id
  AND cf.canonical_code    = :canonical_code
  AND cf.restatement_basis = :restatement_basis
  AND cf.period_type       = :period_type
  AND cf.consolidation     = :consolidation
ORDER BY cf.period_end, cf.period_type, cf.mapping_version DESC
""")

_CPI_SENTINEL = date(9999, 1, 1)


async def get_metric_timeseries(
    session: AsyncSession,
    caller: Principal,
    entity_id: UUID,
    canonical_code: str,
    *,
    restatement_basis: RestatementBasis | None = None,
    period_type: PeriodType,
    consolidation: Consolidation,
    limit: int = 40,
) -> list[TimeseriesPoint]:
    """Return a time-series for a single *canonical_code* on one entity.

    When *restatement_basis* is ``None`` both ``as_reported`` and
    ``cpi_normalized`` values are joined into each
    :class:`TimeseriesPoint`.  When a specific basis is given the other
    column is ``None``.
    """
    base_params: dict[str, object] = {
        "entity_id": entity_id,
        "canonical_code": canonical_code,
        "period_type": period_type.value,
        "consolidation": consolidation.value,
    }

    if restatement_basis is None:
        result = await session.execute(_TS_BOTH_SQL, base_params)
        rows = result.fetchall()
        points = [
            TimeseriesPoint(
                period_end=r.period_end,
                period_type=PeriodType(r.period_type),
                as_reported=r.as_reported,
                cpi_normalized=r.cpi_normalized,
                cpi_base_date=(
                    r.cpi_base_date
                    if r.cpi_base_date is not None and r.cpi_base_date != _CPI_SENTINEL
                    else None
                ),
            )
            for r in rows
        ]
    else:
        params = {**base_params, "restatement_basis": restatement_basis.value}
        result = await session.execute(_TS_SINGLE_SQL, params)
        rows = result.fetchall()
        points = []
        for r in rows:
            is_cpi = r.restatement_basis == RestatementBasis.CPI_NORMALIZED.value
            cpi_bd = r.cpi_base_date if is_cpi and r.cpi_base_date != _CPI_SENTINEL else None
            points.append(
                TimeseriesPoint(
                    period_end=r.period_end,
                    period_type=PeriodType(r.period_type),
                    as_reported=r.value if not is_cpi else None,
                    cpi_normalized=r.value if is_cpi else None,
                    cpi_base_date=cpi_bd,
                )
            )

    return points[:limit]
