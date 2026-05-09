"""TEFAS recency probe.

Source: TEFAS (Türkiye Elektronik Fon Alım Satım Platformu) — Turkey's
mutual-fund trading platform. Primary table: `tefas.fund_holding`.
Recency dimension: `per_fund_cadence` (the rolling-90-day median update
interval per fund — for the v1 cron we treat the source-wide max as
the dimension and let M2 split per-fund views).

`db_latest`: `MAX(snapshot_date)::timestamptz FROM tefas.fund_holding`,
gated on table presence.

`upstream_latest`: placeholder for v1 — returns `now() - 2 days` per
the spec dispatch. TEFAS publishes daily NAV after T+1 settlement, so
"two days ago" is a conservative upper bound on what should already
be in the DB. Marked `# TODO(M1.1)`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq.probes._helpers import absent_detail, table_present
from aslan_core.dq.probes._sql import MAX_TEFAS_SNAPSHOT_DATE


class TefasProbe:
    """Recency probe for the TEFAS fund-holding source."""

    source: str = "tefas"

    async def upstream_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        # TODO(M1.1): replace with the per-fund rolling-90-d median
        # cadence projection. v1 uses `now() - 2 days` as a global
        # upper bound (TEFAS publishes daily NAV after T+1 settlement).
        _ = session, dimension
        upstream = datetime.now(UTC) - timedelta(days=2)
        return upstream, {
            "probe": "placeholder",
            "offset": "2 days",
            "todo": "M1.1: per-fund rolling-90-d median cadence",
        }

    async def db_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        _ = dimension
        if not await table_present(session, "tefas", "fund_holding"):
            return None, absent_detail("tefas", "fund_holding")
        row = (await session.execute(MAX_TEFAS_SNAPSHOT_DATE)).one()
        ts: datetime | None = row.db_latest_at
        return ts, {"table": "tefas.fund_holding", "table_present": True}
