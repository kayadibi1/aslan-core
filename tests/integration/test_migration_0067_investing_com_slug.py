"""Migration 0067 — audit.investing_com_slug + BIST-50 seed.

Asserts the table shape, seed-row count, the GENERATED ``full_url``
column resolves to ``https://tr.investing.com/equities/<slug>``, and
the dashboard role's SELECT-only posture.

The corroborator's ``investing_com`` adapter consults this table at
fetch time to resolve the canonical Investing.com URL for a BIST
ticker (replacing the legacy ``-istanbul-stock-exchange`` redirect
shape that depended on Investing's edge for slug resolution).
"""

from __future__ import annotations

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_investing_com_slug_table_created(engine: AsyncEngine) -> None:
    """The table exists with the expected columns + PK."""
    async with engine.connect() as conn:
        present = (
            await conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT 1 FROM information_schema.tables "
                    "  WHERE table_schema = 'audit' "
                    "    AND table_name = 'investing_com_slug'"
                    ") AS p"
                )
            )
        ).scalar_one()
        assert bool(present), "audit.investing_com_slug not created"
        cols = {
            r.column_name
            for r in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'audit' "
                        "  AND table_name = 'investing_com_slug'"
                    )
                )
            ).all()
        }
    assert {"ticker", "slug", "full_url", "verified_at", "notes"} <= cols


async def test_investing_com_slug_seed_has_50_rows(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        n = (
            await conn.execute(text("SELECT count(*)::int FROM audit.investing_com_slug"))
        ).scalar_one()
    assert n == 50, f"expected 50 seeded rows, got {n}"


async def test_investing_com_slug_full_url_generated_correctly(
    engine: AsyncEngine,
) -> None:
    """The GENERATED ``full_url`` column resolves to
    ``https://tr.investing.com/equities/<slug>`` for a known seed row."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT slug, full_url FROM audit.investing_com_slug WHERE ticker = 'AKBNK'")
            )
        ).one()
    assert row.slug == "akbank"
    assert row.full_url == "https://tr.investing.com/equities/akbank"


async def test_corroborator_slug_lookup_hits_table(engine: AsyncEngine) -> None:
    """``_resolve_investing_slug_url`` returns the seeded full_url
    for a ticker that's in the table, ``None`` for one that isn't."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from aslan_core.dq import corroborator

    sess_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with sess_factory() as s:
        hit = await corroborator._resolve_investing_slug_url(s, entity_ticker="AKBNK")
        miss = await corroborator._resolve_investing_slug_url(s, entity_ticker="NONEXISTENT_TICKER")
    assert hit == "https://tr.investing.com/equities/akbank"
    assert miss is None


async def test_dashboard_role_can_select(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    """aslan_dashboard role has SELECT (the corroborator panel reads
    from this table at render time via the dashboard process)."""
    n = await aslan_dashboard_conn.fetchval("SELECT count(*) FROM audit.investing_com_slug")
    assert int(n) == 50
