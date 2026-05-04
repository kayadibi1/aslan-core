"""Query functions for price and NAV time-series data.

Reads from ts.observation + ts.series_catalog for BIST stock prices
and TEFAS fund NAV data.
"""

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


async def get_stock_prices(
    session: AsyncSession,
    entity_id: UUID,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 500,
) -> list[PricePoint]:
    params: dict[str, object] = {"eid": entity_id, "lim": min(limit, 2000)}
    conditions = ["sc.entity_id = :eid", "sc.source_id = 'bist'"]

    if start:
        conditions.append("o.ts >= :start")
        params["start"] = datetime(start.year, start.month, start.day, tzinfo=UTC)
    if end:
        conditions.append("o.ts <= :end")
        params["end"] = datetime(end.year, end.month, end.day, tzinfo=UTC)

    where = " AND ".join(conditions)

    rows = (
        await session.execute(
            text(
                f"SELECT o.ts::date AS d, sc.metric, o.value "  # noqa: S608
                f"FROM ts.observation o "
                f"JOIN ts.series_catalog sc ON sc.series_id = o.series_id "
                f"WHERE {where} "
                f"ORDER BY o.ts DESC "
                f"LIMIT :lim"
            ),
            params,
        )
    ).all()

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
    conditions = [
        "sc.entity_id = :eid",
        "sc.source_id = 'tefas'",
        "sc.metric = 'nav'",
    ]

    if start:
        conditions.append("o.ts >= :start")
        params["start"] = datetime(start.year, start.month, start.day, tzinfo=UTC)
    if end:
        conditions.append("o.ts <= :end")
        params["end"] = datetime(end.year, end.month, end.day, tzinfo=UTC)

    where = " AND ".join(conditions)

    rows = (
        await session.execute(
            text(
                f"SELECT o.ts::date AS d, o.value "  # noqa: S608
                f"FROM ts.observation o "
                f"JOIN ts.series_catalog sc ON sc.series_id = o.series_id "
                f"WHERE {where} "
                f"ORDER BY o.ts DESC "
                f"LIMIT :lim"
            ),
            params,
        )
    ).all()

    return [NavPoint(ts=r.d, price=Decimal(str(r.value))) for r in rows if r.value is not None]


@dataclass(frozen=True)
class FundamentalSnapshot:
    metric: str
    value: Decimal
    as_of: date


async def get_entity_fundamentals(
    session: AsyncSession,
    entity_id: UUID,
) -> list[FundamentalSnapshot]:
    """Get latest fundamental metrics for an entity (P/E, P/B, market cap, etc.).

    Queries ``ts.observation`` joined with ``ts.series_catalog`` for rows
    where ``source_id='bist'`` and the metric starts with ``fundamental_``.
    Uses ``DISTINCT ON`` to return only the most recent value per metric.
    The ``fundamental_`` prefix is stripped for cleaner output names.
    """
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
    conditions = ["sc.series_code = :sc"]

    if start:
        conditions.append("o.ts >= :start")
        params["start"] = datetime(start.year, start.month, start.day, tzinfo=UTC)
    if end:
        conditions.append("o.ts <= :end")
        params["end"] = datetime(end.year, end.month, end.day, tzinfo=UTC)

    where = " AND ".join(conditions)

    rows = (
        await session.execute(
            text(
                f"SELECT o.ts::date AS d, o.value "  # noqa: S608
                f"FROM ts.observation o "
                f"JOIN ts.series_catalog sc ON sc.series_id = o.series_id "
                f"WHERE {where} "
                f"ORDER BY o.ts DESC "
                f"LIMIT :lim"
            ),
            params,
        )
    ).all()

    return [(r.d, Decimal(str(r.value))) for r in rows if r.value is not None]
