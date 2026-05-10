"""BIST recency probe.

Source: Borsa İstanbul — Turkey's stock exchange. Primary table:
``bist.daily_ohlcv``. Recency dimension: ``trade_close_to_ohlcv``
(the SLA window between exchange close and OHLCV ingest).

``db_latest``: ``MAX(trade_date)::timestamptz FROM bist.daily_ohlcv``,
gated on table presence.

``upstream_latest``: TWO modes, picked at runtime via
``ref.calendar_tr`` presence:

  1. **Calendar mode** (``ref.calendar_tr`` populated): returns the
     most-recent ``trade_date`` where ``is_trading_day=true`` AND
     ``trade_date <= today (Europe/Istanbul)``, plus the BIST
     close-time-of-day (15:00 UTC = 18:00 Europe/Istanbul). This
     correctly excludes TR public holidays (Bayram weeks, Republic
     Day, etc.) — the false-positive "stale" the M1.1 placeholder
     emitted on holiday weeks goes away.

  2. **Mon-Fri fallback** (``ref.calendar_tr`` absent or empty): the
     same naive Mon-Fri-only logic the placeholder used. Migration
     0066 also seeds the next 12 months of TR holidays so a fresh
     deployment that runs migrations to head no longer needs the
     fallback for everyday operation.

Europe/Istanbul is UTC+3 year-round (no DST since 2016). Cash
session closes 18:00 local = 15:00 UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq.probes._helpers import absent_detail, table_present
from aslan_core.dq.probes._sql import MAX_BIST_TRADE_DATE

_BIST_CLOSE_UTC = time(15, 0, tzinfo=UTC)


# Most-recent past trading day from ``ref.calendar_tr``. We anchor on
# ``current_date AT TIME ZONE 'Europe/Istanbul'`` so a sweep at 02:00
# UTC (i.e. 05:00 Istanbul) on a trading day still reports yesterday's
# close as the latest expected trade — today's close hasn't happened
# yet locally.
_LATEST_CALENDAR_TRADE_DATE = text(
    "SELECT MAX(trade_date) AS trade_date "
    "FROM ref.calendar_tr "
    "WHERE is_trading_day = true "
    "  AND trade_date <= (now() AT TIME ZONE 'Europe/Istanbul')::date"
)


def _last_business_day_close_utc(now: datetime) -> datetime:
    """Most-recent past 15:00 UTC weekday timestamp (naive Mon-Fri).

    Used by the fallback when ``ref.calendar_tr`` is absent or empty.
    Will mis-report stale on TR public holidays — the calendar mode
    via migration 0066 is the canonical fix.
    """
    today_close = datetime.combine(now.date(), _BIST_CLOSE_UTC)
    candidate = today_close if now >= today_close else today_close - timedelta(days=1)
    while candidate.weekday() >= 5:  # 5 = Sat, 6 = Sun
        candidate -= timedelta(days=1)
    return candidate


def _trade_date_to_close_utc(trade_date: Any) -> datetime:
    """Map a calendar-table ``trade_date`` (DATE) to the close
    timestamp at 15:00 UTC on that date."""
    return datetime.combine(trade_date, _BIST_CLOSE_UTC)


class BistProbe:
    """Recency probe for the BIST daily-OHLCV source."""

    source: str = "bist"

    async def upstream_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        _ = dimension
        if not await table_present(session, "ref", "calendar_tr"):
            ts = _last_business_day_close_utc(datetime.now(UTC))
            return ts, {
                "probe": "fallback_mon_fri",
                "rule": "last weekday 15:00 UTC (no holiday calendar)",
                "calendar_table_present": False,
            }
        row = (await session.execute(_LATEST_CALENDAR_TRADE_DATE)).one()
        td = row.trade_date
        if td is None:
            ts = _last_business_day_close_utc(datetime.now(UTC))
            return ts, {
                "probe": "fallback_mon_fri",
                "rule": "last weekday 15:00 UTC (calendar empty)",
                "calendar_table_present": True,
                "calendar_empty": True,
            }
        return _trade_date_to_close_utc(td), {
            "probe": "calendar",
            "calendar_table": "ref.calendar_tr",
            "trade_date": td.isoformat(),
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
