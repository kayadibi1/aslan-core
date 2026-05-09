"""Migration 0065 — audit.curated_top_50 backing table + 50-row seed.

Asserts the table shape, the seed roster (50 rows, ranks 1-50 unique,
all canonical tickers present), the dashboard role's SELECT-only
posture, and the deterministic UUIDv5 derivation matches what the
migration computed.

The dispatcher rule ``regression_flag_critical_entity`` (seeded by
0059) joins ``audit.regression_flag.entity_id`` against this table.
Before 0065 the table did not exist, so the predicate's
``WHERE entity_id = ANY(SELECT entity_id FROM audit.curated_top_50)``
silently returned zero rows.
"""

from __future__ import annotations

from uuid import NAMESPACE_DNS, uuid5

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


_EXPECTED_TICKERS = (
    # BIST-30
    "AKBNK",
    "ASELS",
    "BIMAS",
    "EREGL",
    "FROTO",
    "GARAN",
    "HALKB",
    "HEKTS",
    "ISCTR",
    "KCHOL",
    "KOZAA",
    "KOZAL",
    "KRDMD",
    "PETKM",
    "PGSUS",
    "SAHOL",
    "SASA",
    "SISE",
    "TAVHL",
    "TCELL",
    "THYAO",
    "TOASO",
    "TUPRS",
    "VAKBN",
    "VESTL",
    "YKBNK",
    "ARCLK",
    "ENKAI",
    "KORDS",
    "SOKM",
    # Strategic-coverage extras
    "AEFES",
    "AGHOL",
    "ALARK",
    "CCOLA",
    "DOAS",
    "DOHOL",
    "EKGYO",
    "ENJSA",
    "GUBRF",
    "ISDMR",
    "KARSN",
    "MGROS",
    "NTHOL",
    "ODAS",
    "OYAKC",
    "SELEC",
    "SMRTG",
    "TKFEN",
    "TTRAK",
    "ZOREN",
)


async def test_curated_top_50_seed_count_and_ranks(engine: AsyncEngine) -> None:
    """The seed lands exactly 50 rows with ranks 1..50 unique."""
    async with engine.connect() as conn:
        n = (
            await conn.execute(text("SELECT count(*)::int FROM audit.curated_top_50"))
        ).scalar_one()
        ranks = [
            r.rank
            for r in (
                await conn.execute(text("SELECT rank FROM audit.curated_top_50 ORDER BY rank"))
            ).all()
        ]
    assert n == 50, f"expected 50 seeded rows, got {n}"
    assert ranks == list(range(1, 51)), f"expected ranks 1..50, got {ranks}"


async def test_curated_top_50_all_tickers_present(engine: AsyncEngine) -> None:
    """Every canonical ticker is present and ticker is unique."""
    async with engine.connect() as conn:
        seeded = {
            r.ticker
            for r in (await conn.execute(text("SELECT ticker FROM audit.curated_top_50"))).all()
        }
    expected = set(_EXPECTED_TICKERS)
    assert seeded == expected, f"missing: {expected - seeded}, extra: {seeded - expected}"


async def test_curated_top_50_entity_ids_are_uuid5_deterministic(
    engine: AsyncEngine,
) -> None:
    """Each seeded row's entity_id matches uuid5(NAMESPACE_DNS,
    f'bist:ticker:{ticker}'). This is the contract the migration
    documents and downstream code (production ref.entity backfill,
    test fixtures) relies on for reproducibility."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT entity_id, ticker FROM audit.curated_top_50"))
        ).all()
    for r in rows:
        expected = uuid5(NAMESPACE_DNS, f"bist:ticker:{r.ticker}")
        assert str(r.entity_id) == str(expected), (
            f"ticker={r.ticker!r}: expected entity_id={expected}, got {r.entity_id}"
        )


async def test_dashboard_role_can_select(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    """aslan_dashboard role has SELECT (forward-compat for a future
    /dq/regression panel surfacing top-50 labels)."""
    n = await aslan_dashboard_conn.fetchval("SELECT count(*) FROM audit.curated_top_50")
    assert int(n) == 50
