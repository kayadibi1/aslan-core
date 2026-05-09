"""Roundtrip tests for dq.regression — flag, set_status, pending_flags.

Covers:
  * flag() inserts a row with status='open' and returns flag_id
  * set_status() flips status + emits audit.event(regression_flag_reviewed)
  * pending_flags() returns only open rows
  * set_status() raises ValueError for an unknown status
  * set_status() raises LookupError for an unknown flag_id
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.dq import regression

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def _wipe(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM audit.regression_flag"))


async def test_flag_inserts_row_with_open_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await _wipe(s)
        flag_id = await regression.flag(
            session=s,
            source="ts",
            record_table="ts.canonical_financial",
            record_pk={"entity_id": "abc", "canonical_code": "is.revenue"},
            metric="revenue",
            prior_value=Decimal("100"),
            current_value=Decimal("150"),
            shift_pct=Decimal("50"),
            threshold_pct=Decimal("25"),
            detected_at=datetime.now(UTC),
        )
        await s.commit()
        assert flag_id > 0
        row = (
            await s.execute(
                text(
                    "SELECT source, metric, status, shift_pct "
                    "FROM audit.regression_flag WHERE flag_id = :id"
                ),
                {"id": flag_id},
            )
        ).one()
        assert row.source == "ts"
        assert row.metric == "revenue"
        assert row.status == "open"
        assert Decimal(str(row.shift_pct)) == Decimal("50")


async def test_pending_flags_lists_open_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await _wipe(s)
        # 3 open + 1 dismissed
        ids = []
        for i in range(3):
            ids.append(
                await regression.flag(
                    session=s,
                    source="ts",
                    record_table="ts.canonical_financial",
                    record_pk={"i": i},
                    metric="revenue",
                    prior_value=Decimal("100"),
                    current_value=Decimal("200"),
                    shift_pct=Decimal("100"),
                    threshold_pct=Decimal("25"),
                    detected_at=datetime.now(UTC),
                )
            )
        closed_id = await regression.flag(
            session=s,
            source="ts",
            record_table="ts.canonical_financial",
            record_pk={"i": 99},
            metric="revenue",
            prior_value=Decimal("100"),
            current_value=Decimal("200"),
            shift_pct=Decimal("100"),
            threshold_pct=Decimal("25"),
            detected_at=datetime.now(UTC),
        )
        await regression.set_status(
            session=s, flag_id=closed_id, status="dismissed", reviewer="tester"
        )
        await s.commit()

        pending = await regression.pending_flags(session=s, limit=10)
        pending_ids = {p.flag_id for p in pending}
        assert pending_ids == set(ids)


async def test_set_status_emits_audit_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await _wipe(s)
        flag_id = await regression.flag(
            session=s,
            source="ts",
            record_table="ts.canonical_financial",
            record_pk={"entity_id": "abc"},
            metric="revenue",
            prior_value=Decimal("100"),
            current_value=Decimal("200"),
            shift_pct=Decimal("100"),
            threshold_pct=Decimal("25"),
            detected_at=datetime.now(UTC),
        )
        pre_count = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.event "
                    "WHERE event_type = 'regression_flag_reviewed'"
                )
            )
        ).scalar_one()
        await regression.set_status(
            session=s,
            flag_id=flag_id,
            status="confirmed_bug",
            reviewer="sidar",
            review_note="data issue",
        )
        await s.commit()
        post_count = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.event "
                    "WHERE event_type = 'regression_flag_reviewed'"
                )
            )
        ).scalar_one()
        assert int(post_count) == int(pre_count) + 1
        # Status reflects the change
        row = (
            await s.execute(
                text(
                    "SELECT status, reviewer, review_note "
                    "FROM audit.regression_flag WHERE flag_id = :id"
                ),
                {"id": flag_id},
            )
        ).one()
        assert row.status == "confirmed_bug"
        assert row.reviewer == "sidar"
        assert row.review_note == "data issue"


async def test_set_status_rejects_unknown_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await _wipe(s)
        flag_id = await regression.flag(
            session=s,
            source="ts",
            record_table="ts.canonical_financial",
            record_pk={"entity_id": "abc"},
            metric="revenue",
            prior_value=Decimal("100"),
            current_value=Decimal("200"),
            shift_pct=Decimal("100"),
            threshold_pct=Decimal("25"),
            detected_at=datetime.now(UTC),
        )
        with pytest.raises(ValueError, match="unknown status"):
            await regression.set_status(
                session=s, flag_id=flag_id, status="weird-state", reviewer="x"
            )


async def test_set_status_unknown_flag_raises_lookup_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await _wipe(s)
        with pytest.raises(LookupError):
            await regression.set_status(
                session=s, flag_id=999_999_999, status="reviewed", reviewer="x"
            )
