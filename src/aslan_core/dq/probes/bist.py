"""BIST recency probe.

Source: Borsa İstanbul — Turkey's stock exchange. Primary table:
`bist.daily_ohlcv`. Recency dimension: `trade_close_to_ohlcv` (the
SLA window between exchange close and OHLCV ingest).

`db_latest`: `MAX(trade_date)::timestamptz FROM bist.daily_ohlcv`,
gated on table presence.

`upstream_latest`: placeholder for v1 — returns the most recent past
business-day close UTC, computed from `now()`. The real implementation
should consult `ref.calendar_tr` for TR holidays and the BIST trading
session schedule (cash market closes 18:00 Europe/Istanbul on Mon-Fri
exclusive of TR holidays). Marked `# TODO(M1.1)`.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq.probes._helpers import absent_detail, table_present
from aslan_core.dq.probes._sql import MAX_BIST_TRADE_DATE

# Europe/Istanbul is UTC+3 year-round (no DST since 2016). Cash
# session closes at 18:00 local = 15:00 UTC. The placeholder picks
# the most-recent past 15:00 UTC weekday — close enough for v1.
_BIST_CLOSE_UTC = time(15, 0, tzinfo=UTC)


def _last_business_day_close_utc(now: datetime) -> datetime:
    """Return the most-recent past 15:00 UTC weekday timestamp.

    Naive — does NOT consult `ref.calendar_tr` for TR holidays. v1
    accepts the false-positive on Bayram weeks (the placeholder will
    say "upstream is stale" when the exchange is actually closed).
    M1.1 wires the holiday calendar.
    """
    today_close = datetime.combine(now.date(), _BIST_CLOSE_UTC)
    candidate = today_close if now >= today_close else today_close - timedelta(days=1)
    while candidate.weekday() >= 5:  # 5 = Sat, 6 = Sun
        candidate -= timedelta(days=1)
    return candidate


class BistProbe:
    """Recency probe for the BIST daily-OHLCV source."""

    source: str = "bist"

    async def upstream_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        # TODO(M1.1): replace with calendar-aware computation against
        # ref.calendar_tr. The placeholder uses naive Mon-Fri exclusion
        # and will report stale on TR holidays.
        _ = session, dimension
        upstream = _last_business_day_close_utc(datetime.now(UTC))
        return upstream, {
            "probe": "placeholder",
            "rule": "last weekday 15:00 UTC (no holiday calendar)",
            "todo": "M1.1: consult ref.calendar_tr for TR holidays",
        }

    async def db_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        _ = dimension
        if not await table_present(session, "bist", "daily_ohlcv"):
            return None, absent_detail("bist", "daily_ohlcv")
        row = (await session.execute(MAX_BIST_TRADE_DATE)).one()
        ts: datetime | None = row.db_latest_at
        return ts, {"table": "bist.daily_ohlcv", "table_present": True}
