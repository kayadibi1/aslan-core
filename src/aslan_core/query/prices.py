"""Query functions for price, NAV, and fundamentals time-series data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class PricePoint:
    ts: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None


@dataclass(frozen=True)
class NavPoint:
    ts: date
    price: Decimal


@dataclass(frozen=True)
class FundamentalSnapshot:
    metric: str
    value: Decimal
    as_of: date


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


_PRICE_BASE = (
    "SELECT o.ts::date AS d, sc.metric, o.value "
    "FROM ts.observation o "
    "JOIN ts.series_catalog sc ON sc.series_id = o.series_id "
    "WHERE sc.entity_id = :eid AND sc.source_id = 'bist' "
)

_NAV_BASE = (
    "SELECT o.ts::date AS d, o.value "
    "FROM ts.observation o "
    "JOIN ts.series_catalog sc ON sc.series_id = o.series_id "
    "WHERE sc.entity_id = :eid AND sc.source_id = 'tefas' AND sc.metric = 'nav' "
)

_SERIES_BASE = (
    "SELECT o.ts::date AS d, o.value "
    "FROM ts.observation o "
    "JOIN ts.series_catalog sc ON sc.series_id = o.series_id "
    "WHERE sc.series_code = :sc "
)


async def get_stock_prices(
    session: AsyncSession,
    entity_id: UUID,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 500,
) -> list[PricePoint]:
    params: dict[str, object] = {"eid": entity_id, "lim": min(limit, 2000)}

    if start and end:
        q = text(_PRICE_BASE + "AND o.ts >= :start AND o.ts <= :end ORDER BY o.ts DESC LIMIT :lim")
        params["start"] = _dt(start)
        params["end"] = _dt(end)
    elif start:
        q = text(_PRICE_BASE + "AND o.ts >= :start ORDER BY o.ts DESC LIMIT :lim")
        params["start"] = _dt(start)
    elif end:
        q = text(_PRICE_BASE + "AND o.ts <= :end ORDER BY o.ts DESC LIMIT :lim")
        params["end"] = _dt(end)
    else:
        q = text(_PRICE_BASE + "ORDER BY o.ts DESC LIMIT :lim")

    rows = (await session.execute(q, params)).all()

    by_date: dict[date, dict[str, Decimal]] = {}
    for r in rows:
        d = r.d
        if d not in by_date:
            by_date[d] = {}
        by_date[d][r.metric] = Decimal(str(r.value)) if r.value is not None else Decimal(0)

    return [
        PricePoint(
            ts=d,
            open=vals.get("price_open"),
            high=vals.get("price_high"),
            low=vals.get("price_low"),
            close=vals.get("price_close"),
            volume=vals.get("volume"),
        )
        for d, vals in sorted(by_date.items(), reverse=True)
    ]


async def get_fund_nav(
    session: AsyncSession,
    entity_id: UUID,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 500,
) -> list[NavPoint]:
    params: dict[str, object] = {"eid": entity_id, "lim": min(limit, 2000)}

    if start and end:
        q = text(_NAV_BASE + "AND o.ts >= :start AND o.ts <= :end ORDER BY o.ts DESC LIMIT :lim")
        params["start"] = _dt(start)
        params["end"] = _dt(end)
    elif start:
        q = text(_NAV_BASE + "AND o.ts >= :start ORDER BY o.ts DESC LIMIT :lim")
        params["start"] = _dt(start)
    elif end:
        q = text(_NAV_BASE + "AND o.ts <= :end ORDER BY o.ts DESC LIMIT :lim")
        params["end"] = _dt(end)
    else:
        q = text(_NAV_BASE + "ORDER BY o.ts DESC LIMIT :lim")

    rows = (await session.execute(q, params)).all()
    return [NavPoint(ts=r.d, price=Decimal(str(r.value))) for r in rows if r.value is not None]


async def get_entity_fundamentals(
    session: AsyncSession,
    entity_id: UUID,
) -> list[FundamentalSnapshot]:
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT ON (sc.metric) "
                "  sc.metric, o.value, o.ts::date AS d "
                "FROM ts.observation o "
                "JOIN ts.series_catalog sc ON sc.series_id = o.series_id "
                "WHERE sc.entity_id = :eid "
                "  AND sc.source_id = 'bist' "
                "  AND sc.metric LIKE 'fundamental_%' "
                "ORDER BY sc.metric, o.ts DESC"
            ),
            {"eid": entity_id},
        )
    ).all()

    return [
        FundamentalSnapshot(
            metric=r.metric.removeprefix("fundamental_"),
            value=Decimal(str(r.value)),
            as_of=r.d,
        )
        for r in rows
        if r.value is not None
    ]


async def get_observation_series(
    session: AsyncSession,
    series_code: str,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 500,
) -> list[tuple[date, Decimal]]:
    params: dict[str, object] = {"sc": series_code, "lim": min(limit, 2000)}

    if start and end:
        q = text(_SERIES_BASE + "AND o.ts >= :start AND o.ts <= :end ORDER BY o.ts DESC LIMIT :lim")
        params["start"] = _dt(start)
        params["end"] = _dt(end)
    elif start:
        q = text(_SERIES_BASE + "AND o.ts >= :start ORDER BY o.ts DESC LIMIT :lim")
        params["start"] = _dt(start)
    elif end:
        q = text(_SERIES_BASE + "AND o.ts <= :end ORDER BY o.ts DESC LIMIT :lim")
        params["end"] = _dt(end)
    else:
        q = text(_SERIES_BASE + "ORDER BY o.ts DESC LIMIT :lim")

    rows = (await session.execute(q, params)).all()
    return [(r.d, Decimal(str(r.value))) for r in rows if r.value is not None]
