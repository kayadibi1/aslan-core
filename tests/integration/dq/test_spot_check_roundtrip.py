"""Integration tests for `aslan_core.dq.spot_check`.

Exercises:
  * draw_sample() against a synthesized kap.disclosures table (the bare
    testcontainer doesn't carry the puller schemas; we DDL the minimum
    shape inline so the random-pick path is tested end-to-end).
  * draw_sample() falls back to an empty list + emits an event when the
    primary table is absent.
  * draw_sample() with stratum='high_priority_event_type' over-samples
    the high-priority event types relative to a uniform draw.
  * label_field() persists the result row, computes matches +
    variance_pct correctly for numeric and string values, and flips
    spot_check_sample.labelled on first label.
  * mark_sample_complete() is idempotent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.dq import spot_check

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _kap_disclosures_seeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Seed kap.disclosures with a synthetic mix of high-priority and
    other event types so the stratified-draw test has signal to detect."""
    async with session_factory() as s:
        await s.execute(text("CREATE SCHEMA IF NOT EXISTS kap"))
        await s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS kap.disclosures ("
                "  disclosure_id BIGSERIAL PRIMARY KEY, "
                "  event_type TEXT NOT NULL, "
                "  published_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            )
        )
        await s.execute(text("DELETE FROM kap.disclosures"))
        # 20 high-priority rows + 80 background rows. With
        # stratified draw the high-priority portion of a 50-row pick
        # should noticeably exceed 20%.
        for _ in range(20):
            await s.execute(
                text("INSERT INTO kap.disclosures(event_type) VALUES ('material_event')")
            )
        for _ in range(80):
            await s.execute(
                text("INSERT INTO kap.disclosures(event_type) VALUES ('routine_filing')")
            )
        await s.execute(text("DELETE FROM audit.spot_check_result"))
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.spot_check_result"))
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        await s.execute(text("DROP TABLE IF EXISTS kap.disclosures"))
        await s.execute(text("DROP SCHEMA IF EXISTS kap CASCADE"))
        await s.commit()


async def test_draw_sample_writes_rows(
    _kap_disclosures_seeded: None,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """draw_sample inserts N rows into audit.spot_check_sample."""
    async with session_factory() as s:
        sample_ids = await spot_check.draw_sample(session=s, source="kap", n=10)
        await s.commit()
    assert len(sample_ids) == 10
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT source, record_table, labelled FROM audit.spot_check_sample "
                    "ORDER BY drawn_at DESC LIMIT 10"
                )
            )
        ).all()
    assert len(rows) == 10
    for r in rows:
        assert r.source == "kap"
        assert r.record_table == "kap.disclosures"
        assert r.labelled is False


async def test_draw_sample_skips_when_table_absent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """source with no primary table → empty list + event."""
    async with session_factory() as s:
        await s.execute(
            text("DELETE FROM audit.event WHERE event_type = 'spot_check_draw_skipped'")
        )
        ids = await spot_check.draw_sample(session=s, source="tefas", n=5)
        await s.commit()
    assert ids == []
    async with session_factory() as s:
        events = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.event "
                    "WHERE event_type = 'spot_check_draw_skipped'"
                )
            )
        ).scalar_one()
    assert events >= 1


async def test_draw_sample_high_priority_stratum_oversamples(
    _kap_disclosures_seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With 20% high-priority population and 50-row stratified draws,
    the high-priority share should average meaningfully > 20% across
    repeats. Single-trial randomness is too noisy; we run 5 draws and
    aggregate."""
    high_priority_count = 0
    total_drawn = 0
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        await s.commit()
    for _ in range(5):
        async with session_factory() as s:
            await spot_check.draw_sample(
                session=s,
                source="kap",
                n=50,
                stratum="high_priority_event_type",
            )
            await s.commit()
    async with session_factory() as s:
        result = (
            await s.execute(
                text(
                    "SELECT d.event_type, count(*)::int AS n "
                    "FROM audit.spot_check_sample s "
                    "JOIN kap.disclosures d "
                    "  ON d.disclosure_id = (s.record_pk->>'disclosure_id')::bigint "
                    "WHERE s.stratum = 'high_priority_event_type' "
                    "GROUP BY d.event_type"
                )
            )
        ).all()
    for r in result:
        total_drawn += int(r.n)
        if r.event_type == "material_event":
            high_priority_count += int(r.n)
    # Uniform: 20% high-priority. 2× weight → expected ~33% (2*20 / (2*20+80)).
    # We assert > 25% to leave generous statistical headroom for 5 trials of 50.
    assert total_drawn > 0
    assert high_priority_count / total_drawn > 0.25, (
        f"high-priority share {high_priority_count}/{total_drawn} did not "
        f"clear the over-sampling floor"
    )


async def test_label_field_numeric_match(
    _kap_disclosures_seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Numeric db_value == truth_value → matches=true, variance_pct=0."""
    async with session_factory() as s:
        ids = await spot_check.draw_sample(session=s, source="kap", n=1)
        await s.commit()
    assert len(ids) == 1
    sample_id = ids[0]
    async with session_factory() as s:
        result_id = await spot_check.label_field(
            session=s,
            sample_id=sample_id,
            field="net_income_try",
            db_value="123456.78",
            truth_value="123456.78",
            labeller="sidar",
            label_note="exact",
        )
        await s.commit()
    assert result_id > 0
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT field, matches, variance_pct, labeller "
                    "FROM audit.spot_check_result WHERE result_id = :rid"
                ),
                {"rid": result_id},
            )
        ).one()
    assert row.field == "net_income_try"
    assert row.matches is True
    assert Decimal(row.variance_pct) == Decimal(0)
    assert row.labeller == "sidar"


async def test_label_field_numeric_mismatch_computes_variance(
    _kap_disclosures_seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """db=100, truth=110 → matches=false, variance_pct = 100*10/110 ≈ 9.09."""
    async with session_factory() as s:
        ids = await spot_check.draw_sample(session=s, source="kap", n=1)
        await s.commit()
    sample_id = ids[0]
    async with session_factory() as s:
        result_id = await spot_check.label_field(
            session=s,
            sample_id=sample_id,
            field="revenue_try",
            db_value="100",
            truth_value="110",
            labeller="sidar",
        )
        await s.commit()
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT matches, variance_pct FROM audit.spot_check_result "
                    "WHERE result_id = :rid"
                ),
                {"rid": result_id},
            )
        ).one()
    assert row.matches is False
    variance = Decimal(row.variance_pct)
    expected = (Decimal(10) / Decimal(110)) * Decimal(100)
    assert abs(variance - expected) < Decimal("0.001")


async def test_label_field_string_no_variance(
    _kap_disclosures_seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Non-numeric field → matches=string-equality; variance_pct=NULL."""
    async with session_factory() as s:
        ids = await spot_check.draw_sample(session=s, source="kap", n=2)
        await s.commit()
    async with session_factory() as s:
        match_id = await spot_check.label_field(
            session=s,
            sample_id=ids[0],
            field="ticker",
            db_value="GARAN",
            truth_value="GARAN",
            labeller="sidar",
        )
        mismatch_id = await spot_check.label_field(
            session=s,
            sample_id=ids[1],
            field="ticker",
            db_value="GARAN",
            truth_value="AKBNK",
            labeller="sidar",
        )
        await s.commit()
    async with session_factory() as s:
        match_row = (
            await s.execute(
                text(
                    "SELECT matches, variance_pct FROM audit.spot_check_result "
                    "WHERE result_id = :rid"
                ),
                {"rid": match_id},
            )
        ).one()
        mismatch_row = (
            await s.execute(
                text(
                    "SELECT matches, variance_pct FROM audit.spot_check_result "
                    "WHERE result_id = :rid"
                ),
                {"rid": mismatch_id},
            )
        ).one()
    assert match_row.matches is True
    assert match_row.variance_pct is None
    assert mismatch_row.matches is False
    assert mismatch_row.variance_pct is None


async def test_label_field_flips_labelled_flag(
    _kap_disclosures_seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """First label flips spot_check_sample.labelled to true."""
    async with session_factory() as s:
        ids = await spot_check.draw_sample(session=s, source="kap", n=1)
        await s.commit()
    sample_id = ids[0]
    async with session_factory() as s:
        before = (
            await s.execute(
                text(
                    "SELECT labelled, labeller FROM audit.spot_check_sample WHERE sample_id = :sid"
                ),
                {"sid": sample_id},
            )
        ).one()
    assert before.labelled is False
    assert before.labeller is None
    async with session_factory() as s:
        await spot_check.label_field(
            session=s,
            sample_id=sample_id,
            field="x",
            db_value="1",
            truth_value="1",
            labeller="sidar",
        )
        await s.commit()
    async with session_factory() as s:
        after = (
            await s.execute(
                text(
                    "SELECT labelled, labeller, labelled_at "
                    "FROM audit.spot_check_sample WHERE sample_id = :sid"
                ),
                {"sid": sample_id},
            )
        ).one()
    assert after.labelled is True
    assert after.labeller == "sidar"
    assert after.labelled_at is not None


async def test_pending_samples_filters_labelled(
    _kap_disclosures_seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """pending_samples returns only labelled=false rows."""
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.spot_check_result"))
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        ids = await spot_check.draw_sample(session=s, source="kap", n=3)
        await s.commit()
    async with session_factory() as s:
        # Label one of the three.
        await spot_check.label_field(
            session=s,
            sample_id=ids[0],
            field="x",
            db_value="1",
            truth_value="1",
            labeller="sidar",
        )
        await s.commit()
    async with session_factory() as s:
        pending = await spot_check.pending_samples(session=s, source="kap")
    pending_ids = {p.sample_id for p in pending}
    assert ids[0] not in pending_ids
    assert ids[1] in pending_ids
    assert ids[2] in pending_ids


async def test_mark_sample_complete_idempotent(
    _kap_disclosures_seeded: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """mark_sample_complete twice is a no-op on the second call."""
    async with session_factory() as s:
        ids = await spot_check.draw_sample(session=s, source="kap", n=1)
        await s.commit()
    sample_id = ids[0]
    async with session_factory() as s:
        await spot_check.mark_sample_complete(session=s, sample_id=sample_id, labeller="auto")
        await s.commit()
    async with session_factory() as s:
        first = (
            await s.execute(
                text(
                    "SELECT labelled_at, labeller FROM audit.spot_check_sample "
                    "WHERE sample_id = :sid"
                ),
                {"sid": sample_id},
            )
        ).one()
    assert first.labelled_at is not None
    assert first.labeller == "auto"
    # Second call: WHERE labelled = false guard makes this a no-op.
    async with session_factory() as s:
        await spot_check.mark_sample_complete(session=s, sample_id=sample_id, labeller="other")
        await s.commit()
    async with session_factory() as s:
        second = (
            await s.execute(
                text(
                    "SELECT labelled_at, labeller FROM audit.spot_check_sample "
                    "WHERE sample_id = :sid"
                ),
                {"sid": sample_id},
            )
        ).one()
    assert second.labelled_at == first.labelled_at
    assert second.labeller == "auto"


async def test_label_field_unknown_sample_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Labeling a non-existent sample id raises LookupError."""
    from uuid import uuid4

    async with session_factory() as s:
        with pytest.raises(LookupError):
            await spot_check.label_field(
                session=s,
                sample_id=uuid4(),
                field="x",
                db_value="1",
                truth_value="1",
                labeller="sidar",
            )


async def test_draw_sample_unknown_source_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        with pytest.raises(ValueError, match="unknown source"):
            await spot_check.draw_sample(session=s, source="bogus", n=5)


async def test_draw_sample_unsupported_stratum_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        with pytest.raises(ValueError, match="unsupported stratum"):
            await spot_check.draw_sample(session=s, source="kap", n=5, stratum="bogus")


async def test_draw_sample_kap_stratum_only_for_kap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        with pytest.raises(ValueError, match="KAP-only"):
            await spot_check.draw_sample(
                session=s, source="bist", n=5, stratum="high_priority_event_type"
            )
