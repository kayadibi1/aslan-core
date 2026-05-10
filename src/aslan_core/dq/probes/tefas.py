"""TEFAS recency probe.

Source: TEFAS (Türkiye Elektronik Fon Alım Satım Platformu) —
Turkey's mutual-fund trading platform. Primary table:
``tefas.fund_holding``. Recency dimension: ``per_fund_cadence``.

``upstream_latest``: per-fund rolling 90-day median update interval
projected forward from each fund's most-recent snapshot. Across all
funds we take ``max(median_interval)`` (the slowest fund's natural
cadence) — conservative for v1 because it under-alerts rather than
over-alerts. The alternative (``min(median_interval)``, the
fastest-cadence fund determines "due"-ness) would over-alert during
TEFAS publishing slowdowns. The choice is documented inline; M2
should split into per-fund recency rows.

The SQL:

  WITH per_fund_intervals AS (
      SELECT fund_id, snapshot_date,
             snapshot_date - LAG(snapshot_date) OVER (PARTITION BY
               fund_id ORDER BY snapshot_date) AS gap
      FROM tefas.fund_holding
      WHERE snapshot_date >= now() - INTERVAL '90 days'
  ),
  median_per_fund AS (
      SELECT fund_id,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY gap) AS median_interval
      FROM per_fund_intervals
      WHERE gap IS NOT NULL
      GROUP BY fund_id
  )
  SELECT now() - max(median_interval) AS upstream_latest_at
  FROM median_per_fund;

When the table holds zero rows in the trailing 90 days the query
returns NULL — the probe falls back to ``MAX(snapshot_date)`` so the
cron still has a usable upstream signal (DB-only behaviour).

``db_latest``: per-fund ``MAX(snapshot_date)`` aggregated to a
single value via ``MIN`` across funds — a single fund being stale
should pull the metric.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq.probes._helpers import absent_detail, table_present


# now() - max(median_interval per fund). NULL when no rows in window.
_TEFAS_UPSTREAM_BY_MEDIAN = text(
    "WITH per_fund_intervals AS ( "
    "  SELECT fund_id, snapshot_date, "
    "         snapshot_date - LAG(snapshot_date) OVER ( "
    "           PARTITION BY fund_id ORDER BY snapshot_date "
    "         ) AS gap "
    "  FROM tefas.fund_holding "
    "  WHERE snapshot_date >= now() - INTERVAL '90 days' "
    "), "
    "median_per_fund AS ( "
    "  SELECT fund_id, "
    "         percentile_cont(0.5) WITHIN GROUP (ORDER BY gap) AS median_interval "
    "  FROM per_fund_intervals "
    "  WHERE gap IS NOT NULL "
    "  GROUP BY fund_id "
    ") "
    "SELECT (now() - max(median_interval))::timestamptz AS upstream_latest_at, "
    "       count(*)::int AS fund_count, "
    "       max(median_interval) AS slowest_median_interval "
    "FROM median_per_fund"
)


# Per-fund MAX(snapshot_date) aggregated via MIN across funds. We
# convert ``date`` to ``timestamptz`` so the recency_observation lag
# computation (TIMESTAMPTZ - TIMESTAMPTZ) types correctly.
_TEFAS_DB_LATEST_MIN_OF_MAX = text(
    "WITH per_fund_max AS ( "
    "  SELECT fund_id, MAX(snapshot_date) AS max_date "
    "  FROM tefas.fund_holding "
    "  GROUP BY fund_id "
    ") "
    "SELECT MIN(max_date)::timestamptz AS db_latest_at, "
    "       count(*)::int AS fund_count "
    "FROM per_fund_max"
)


class TefasProbe:
    """Recency probe for the TEFAS fund-holding source."""

    source: str = "tefas"

    async def upstream_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        _ = dimension
        if not await table_present(session, "tefas", "fund_holding"):
            return None, absent_detail("tefas", "fund_holding")
        row = (await session.execute(_TEFAS_UPSTREAM_BY_MEDIAN)).one()
        ts: datetime | None = row.upstream_latest_at
        slowest = row.slowest_median_interval
        if ts is None:
            return None, {
                "probe": "rolling_90d_median",
                "rule": "no rows in trailing-90d window",
                "table_present": True,
                "fund_count": int(row.fund_count or 0),
            }
        return ts, {
            "probe": "rolling_90d_median",
            "table": "tefas.fund_holding",
            "fund_count": int(row.fund_count or 0),
            "slowest_median_interval": str(slowest) if slowest is not None else None,
            "aggregate": "max(median_interval) across funds",
        }

    async def db_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        _ = dimension
        if not await table_present(session, "tefas", "fund_holding"):
            return None, absent_detail("tefas", "fund_holding")
        row = (await session.execute(_TEFAS_DB_LATEST_MIN_OF_MAX)).one()
        ts: datetime | None = row.db_latest_at
        return ts, {
            "table": "tefas.fund_holding",
            "table_present": True,
            "fund_count": int(row.fund_count or 0),
            "aggregate": "min(per-fund max(snapshot_date))",
        }
