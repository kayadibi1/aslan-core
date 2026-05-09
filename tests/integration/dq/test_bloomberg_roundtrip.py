"""Integration tests for `aslan_core.dq.bloomberg`.

Exercises:
  * open_quarter creates exactly 60 cells (5 entities x 12 fields).
  * open_quarter is idempotent on re-call.
  * record_bloomberg_value updates the cell.
  * record_aslan_value with a synthetic ts.canonical_financial fixture.
  * record_aslan_value emits a placeholder event when the sampler is a stub.
  * close_quarter refuses on NULL bloomberg_value, succeeds when complete.
  * claim_check returns the most-recent CLOSED-run aggregate.
  * render_markdown produces a structured markdown block.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.dq import bloomberg

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _wipe_bloomberg(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.bloomberg_comparison_cell"))
        await s.execute(text("DELETE FROM audit.bloomberg_comparison_run"))
        await s.execute(text("DELETE FROM audit.event WHERE event_type LIKE 'bloomberg_%'"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.bloomberg_comparison_cell"))
        await s.execute(text("DELETE FROM audit.bloomberg_comparison_run"))
        await s.execute(text("DELETE FROM audit.event WHERE event_type LIKE 'bloomberg_%'"))
        await s.commit()


async def test_open_quarter_creates_60_cells(
    _wipe_bloomberg: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        run_id = await bloomberg.open_quarter(session=s, quarter="2026Q2")
        await s.commit()
    async with session_factory() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.bloomberg_comparison_cell "
                    "WHERE run_id = :rid"
                ),
                {"rid": run_id},
            )
        ).scalar_one()
    assert n == bloomberg.CELLS_PER_RUN
    assert n == 60
    # All cells start with NULL bloomberg_value AND NULL aslan_value.
    async with session_factory() as s:
        nulls = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.bloomberg_comparison_cell "
                    "WHERE run_id = :rid "
                    "  AND bloomberg_value IS NULL AND aslan_value IS NULL"
                ),
                {"rid": run_id},
            )
        ).scalar_one()
    assert nulls == 60


async def test_open_quarter_idempotent(
    _wipe_bloomberg: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        first = await bloomberg.open_quarter(session=s, quarter="2026Q3")
        await s.commit()
    async with session_factory() as s:
        second = await bloomberg.open_quarter(session=s, quarter="2026Q3")
        await s.commit()
    assert first == second
    async with session_factory() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.bloomberg_comparison_cell "
                    "WHERE run_id = :rid"
                ),
                {"rid": first},
            )
        ).scalar_one()
    assert n == 60  # Not 120 — the second call must not re-insert.


async def test_record_bloomberg_value(
    _wipe_bloomberg: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await bloomberg.open_quarter(session=s, quarter="2026Q4")
        await s.commit()
    async with session_factory() as s:
        cell = (
            await s.execute(
                text(
                    "SELECT cell_id FROM audit.bloomberg_comparison_cell "
                    "WHERE entity_ticker = 'AKBNK' AND field = 'revenue_q-1' LIMIT 1"
                )
            )
        ).one()
    async with session_factory() as s:
        await bloomberg.record_bloomberg_value(
            session=s,
            cell_id=cell.cell_id,
            bloomberg_value="1000000000",
            entered_by="sidar",
        )
        await s.commit()
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT bloomberg_value, bloomberg_entered_by "
                    "FROM audit.bloomberg_comparison_cell WHERE cell_id = :cid"
                ),
                {"cid": cell.cell_id},
            )
        ).one()
    assert row.bloomberg_value == "1000000000"
    assert row.bloomberg_entered_by == "sidar"


async def test_record_bloomberg_value_unknown_cell_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        with pytest.raises(LookupError):
            await bloomberg.record_bloomberg_value(
                session=s,
                cell_id=uuid4(),
                bloomberg_value="x",
                entered_by="sidar",
            )


@pytest_asyncio.fixture(loop_scope="session")
async def _seed_canonical_financial(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Seed one entity (AKBNK) with one quarter of revenue + net_income.

    The full schema requires a real ref.entity row + ref.identifier row
    + a synthetic src.ingestion_run row + ref.currency. We seed the
    minimum so the sampler can resolve AKBNK -> entity_id and read one
    revenue + net_income value.
    """
    async with session_factory() as s:
        eid = uuid4()
        # Need a non-null ingestion_run_id for ref.entity itself.
        run_row = (
            await s.execute(text("SELECT ingestion_run_id FROM src.ingestion_run LIMIT 1"))
        ).one_or_none()
        if run_row is None:
            # Seed src.source first (FK target).
            await s.execute(
                text(
                    "INSERT INTO src.source(source_id, name, kind, license_status) "
                    "VALUES ('bist', 'BIST', 'exchange', 'public') "
                    "ON CONFLICT DO NOTHING"
                )
            )
            run_row = (
                await s.execute(
                    text(
                        "INSERT INTO src.ingestion_run("
                        "  source_id, job_name, status, started_at"
                        ") VALUES ('bist', 'test-seed', 'succeeded', now()) "
                        "RETURNING ingestion_run_id"
                    )
                )
            ).one()
        run_id = run_row.ingestion_run_id

        # ref.entity / ref.identifier
        await s.execute(
            text(
                "INSERT INTO ref.entity(entity_id, entity_type, status, "
                "  legal_name, country_code, source_id, ingestion_run_id) "
                "VALUES (:eid, 'company', 'active', 'Akbank Test', 'TR', "
                "        'bist', :rid) "
                "ON CONFLICT DO NOTHING"
            ),
            {"eid": eid, "rid": run_id},
        )
        # Make sure there's no pre-existing AKBNK entry that would
        # confuse the sampler.
        await s.execute(
            text("DELETE FROM ref.identifier WHERE namespace = 'bist_ticker' AND value = 'AKBNK'")
        )
        await s.execute(
            text(
                "INSERT INTO ref.identifier"
                "(entity_id, namespace, value, valid_from, valid_to, "
                " is_primary, source_id, ingestion_run_id) "
                "VALUES (:eid, 'bist_ticker', 'AKBNK', '2020-01-01', "
                "        '9999-12-31', true, 'bist', :rid)"
            ),
            {"eid": eid, "rid": run_id},
        )
        # Seed ref.currency if needed.
        await s.execute(
            text(
                "INSERT INTO ref.currency(currency_code, name) "
                "VALUES ('TRY', 'Turkish Lira') ON CONFLICT DO NOTHING"
            )
        )
        # 4 quarterly revenues + 4 quarterly net_income for AKBNK
        for n, period_end in enumerate(
            [
                date(2026, 3, 31),
                date(2025, 12, 31),
                date(2025, 9, 30),
                date(2025, 6, 30),
            ]
        ):
            for code, value in [
                ("is.revenue", Decimal("1000000000") + Decimal(n) * Decimal("100")),
                (
                    "is.net_income",
                    Decimal("200000000") + Decimal(n) * Decimal("10"),
                ),
            ]:
                await s.execute(
                    text(
                        "INSERT INTO ts.canonical_financial("
                        "  entity_id, canonical_code, period_end, period_type, "
                        "  consolidation, restatement_basis, currency_code, "
                        "  accounting_standard, value, payload_hash, "
                        "  source_contributions, mapping_version, manifest_hash, "
                        "  as_of, ingestion_run_id"
                        ") VALUES ("
                        "  :eid, :code, :pe, 'q', 'consolidated', 'as_reported', "
                        "  'TRY', 'IFRS', :v, "
                        "  repeat('a', 64), '{}'::jsonb, 1, "
                        "  repeat('b', 64), now(), :rid"
                        ") ON CONFLICT DO NOTHING"
                    ),
                    {"eid": eid, "code": code, "pe": period_end, "v": value, "rid": run_id},
                )
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(
            text(
                "DELETE FROM ts.canonical_financial WHERE entity_id IN "
                "(SELECT entity_id FROM ref.identifier "
                " WHERE namespace = 'bist_ticker' AND value = 'AKBNK')"
            )
        )
        await s.execute(
            text("DELETE FROM ref.identifier WHERE namespace = 'bist_ticker' AND value = 'AKBNK'")
        )
        await s.commit()


async def test_record_aslan_value_revenue_from_canonical(
    _wipe_bloomberg: None,
    _seed_canonical_financial: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The auto-sampler reads ts.canonical_financial for AKBNK + populates
    the cell."""
    async with session_factory() as s:
        await bloomberg.open_quarter(session=s, quarter="2026Q1")
        await s.commit()
    async with session_factory() as s:
        cell = (
            await s.execute(
                text(
                    "SELECT cell_id FROM audit.bloomberg_comparison_cell "
                    "WHERE entity_ticker = 'AKBNK' AND field = 'revenue_q-1' LIMIT 1"
                )
            )
        ).one()
    async with session_factory() as s:
        await bloomberg.record_aslan_value(session=s, cell_id=cell.cell_id)
        await s.commit()
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT aslan_value, aslan_advantage, aslan_sampled_at "
                    "FROM audit.bloomberg_comparison_cell WHERE cell_id = :cid"
                ),
                {"cid": cell.cell_id},
            )
        ).one()
    assert row.aslan_value is not None
    # AKBNK Q-1 revenue seeded as 1000000000.
    assert Decimal(row.aslan_value) == Decimal("1000000000")
    # Bloomberg side is NULL → wins.
    assert row.aslan_advantage == "wins"
    assert row.aslan_sampled_at is not None


async def test_record_aslan_value_placeholder_emits_event(
    _wipe_bloomberg: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """latest_dividend_amount sampler is a placeholder -> bloomberg_sampler_placeholder event."""
    async with session_factory() as s:
        await bloomberg.open_quarter(session=s, quarter="2026Q1")
        await s.commit()
    async with session_factory() as s:
        cell = (
            await s.execute(
                text(
                    "SELECT cell_id FROM audit.bloomberg_comparison_cell "
                    "WHERE entity_ticker = 'AKBNK' AND field = 'latest_dividend_amount' "
                    "LIMIT 1"
                )
            )
        ).one()
    async with session_factory() as s:
        before = (
            await s.execute(
                text(
                    "SELECT count(*)::int FROM audit.event "
                    "WHERE event_type = 'bloomberg_sampler_placeholder'"
                )
            )
        ).scalar_one()
    async with session_factory() as s:
        await bloomberg.record_aslan_value(session=s, cell_id=cell.cell_id)
        await s.commit()
    async with session_factory() as s:
        after = (
            await s.execute(
                text(
                    "SELECT count(*)::int FROM audit.event "
                    "WHERE event_type = 'bloomberg_sampler_placeholder'"
                )
            )
        ).scalar_one()
    assert int(after) == int(before) + 1


async def test_close_quarter_refuses_with_null_bloomberg_cells(
    _wipe_bloomberg: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await bloomberg.open_quarter(session=s, quarter="2026Q1")
        await s.commit()
    with pytest.raises(bloomberg.QuarterNotReadyError) as exc_info:
        async with session_factory() as s:
            await bloomberg.close_quarter(session=s, quarter="2026Q1")
            await s.commit()
    assert exc_info.value.null_cell_count == 60


async def test_close_quarter_succeeds_when_all_filled(
    _wipe_bloomberg: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await bloomberg.open_quarter(session=s, quarter="2026Q1")
        await s.commit()
    # Fill every bloomberg cell with a placeholder string.
    async with session_factory() as s:
        await s.execute(
            text(
                "UPDATE audit.bloomberg_comparison_cell SET "
                "  bloomberg_value = 'placeholder', "
                "  bloomberg_entered_by = 'test', "
                "  bloomberg_entered_at = now()"
            )
        )
        await s.commit()
    async with session_factory() as s:
        await bloomberg.close_quarter(session=s, quarter="2026Q1", closed_by="cli:test")
        await s.commit()
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT closed_at, closed_by FROM audit.bloomberg_comparison_run "
                    "WHERE quarter = '2026Q1'"
                )
            )
        ).one()
    assert row.closed_at is not None
    assert row.closed_by == "cli:test"


async def test_close_quarter_unknown_quarter_raises(
    _wipe_bloomberg: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        with pytest.raises(LookupError):
            await bloomberg.close_quarter(session=s, quarter="1999Q4")


async def test_claim_check_no_closed_run(
    _wipe_bloomberg: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        cc = await bloomberg.claim_check(session=s, field="revenue_q-1")
    assert cc.quarter is None
    assert cc.wins == 0
    assert cc.ties == 0
    assert cc.loses == 0
    assert cc.cells == ()


async def test_claim_check_aggregates_closed_run(
    _wipe_bloomberg: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Open a quarter, force-fill cells with mixed advantages, close, run claim_check."""
    async with session_factory() as s:
        await bloomberg.open_quarter(session=s, quarter="2026Q1")
        await s.commit()
    # Fill all cells with bloomberg values + advantages by hand.
    async with session_factory() as s:
        await s.execute(
            text(
                "UPDATE audit.bloomberg_comparison_cell SET "
                "  bloomberg_value = '100', "
                "  bloomberg_entered_by = 'test', "
                "  bloomberg_entered_at = now(), "
                "  aslan_value = '100', "
                "  aslan_advantage = 'ties' "
                "WHERE field = 'revenue_q-1'"
            )
        )
        # AKBNK wins, others tie. 1 win + 4 ties.
        await s.execute(
            text(
                "UPDATE audit.bloomberg_comparison_cell SET aslan_advantage = 'wins' "
                "WHERE entity_ticker = 'AKBNK' AND field = 'revenue_q-1'"
            )
        )
        # Fill the remaining cells with bloomberg values so close passes.
        await s.execute(
            text(
                "UPDATE audit.bloomberg_comparison_cell SET "
                "  bloomberg_value = 'x', "
                "  bloomberg_entered_by = 'test', "
                "  bloomberg_entered_at = now() "
                "WHERE bloomberg_value IS NULL"
            )
        )
        await s.commit()
    async with session_factory() as s:
        await bloomberg.close_quarter(session=s, quarter="2026Q1")
        await s.commit()
    async with session_factory() as s:
        cc = await bloomberg.claim_check(session=s, field="revenue_q-1")
    assert cc.quarter == "2026Q1"
    assert cc.wins == 1
    assert cc.ties == 4
    assert cc.loses == 0
    assert len(cc.cells) == 5
    # PR text should mention every entity in canonical order.
    pr = cc.to_pr_text()
    for entity in bloomberg.ANCHOR_ENTITIES:
        assert entity in pr


async def test_claim_check_unknown_field_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        with pytest.raises(ValueError, match="unknown field"):
            await bloomberg.claim_check(session=s, field="bogus")


async def test_render_markdown_emits_per_entity_tables(
    _wipe_bloomberg: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await bloomberg.open_quarter(session=s, quarter="2026Q1")
        await s.commit()
    async with session_factory() as s:
        await s.execute(
            text(
                "UPDATE audit.bloomberg_comparison_cell SET "
                "  bloomberg_value = 'b', "
                "  aslan_value = 'a', "
                "  aslan_advantage = 'wins'"
            )
        )
        await bloomberg.close_quarter(session=s, quarter="2026Q1")
        await s.commit()
    async with session_factory() as s:
        run = await bloomberg.latest_closed_run(session=s)
    assert run is not None
    md = bloomberg.render_markdown(run)
    assert "Bloomberg vs Aslan" in md
    assert "2026Q1" in md
    for entity in bloomberg.ANCHOR_ENTITIES:
        assert f"### {entity}" in md
    # 60 wins claimed.
    assert "60 wins" in md


async def test_parse_quarter_and_helpers() -> None:
    assert bloomberg.parse_quarter("2026Q2") == (2026, 2)
    with pytest.raises(ValueError):
        bloomberg.parse_quarter("not-a-quarter")

    # current_quarter mid-Q2.
    q = bloomberg.current_quarter(now=datetime(2026, 5, 1, tzinfo=UTC))
    assert q == "2026Q2"

    assert bloomberg.n_quarters_back("2026Q2", 1) == "2026Q1"
    assert bloomberg.n_quarters_back("2026Q1", 1) == "2025Q4"
    assert bloomberg.n_quarters_back("2026Q1", 5) == "2024Q4"


async def test_compute_advantage_branches() -> None:
    """Direct unit-style check of the advantage heuristic."""
    from aslan_core.dq.bloomberg import _compute_advantage

    # Both null
    assert _compute_advantage(None, None) == (None, "ties")
    # Aslan-only
    v, adv = _compute_advantage(None, "100")
    assert (v, adv) == (None, "wins")
    # Bloomberg-only
    v, adv = _compute_advantage("100", None)
    assert (v, adv) == (None, "loses")
    # Both numeric, exact match
    v, adv = _compute_advantage("100", "100")
    assert adv == "ties"
    # Both numeric, within 1%
    v, adv = _compute_advantage("100", "100.5")
    assert adv == "ties"
    # Both numeric, > 1% diff
    v, adv = _compute_advantage("100", "200")
    assert adv == "loses"
    assert v is not None and v > Decimal(1)
    # Both string, exact match
    assert _compute_advantage("foo", "foo") == (None, "ties")
    assert _compute_advantage("foo", "bar") == (None, "loses")
