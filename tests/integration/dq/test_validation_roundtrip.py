"""Roundtrip test for dq.validation.check()."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.dq import validation
from aslan_core.dq.rules import Rule
from aslan_core.dq.types import Severity

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


# A throwaway test rule registered for this test only.
def _missing_revenue(record: dict[str, Any]) -> list[validation.RuleResult]:
    if "revenue" not in record:
        return [
            validation.RuleResult(
                rule_name="test_missing_revenue",
                severity=Severity.WARN,
                detail={"missing_field": "revenue"},
            )
        ]
    return []


_TEST_RULES = {
    "ts.canonical_financial": [
        Rule(
            name="test_missing_revenue",
            run=_missing_revenue,
        )
    ],
}


async def test_failing_record_records_failure_row(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    record = {"entity_id": 1, "as_of": "2026-01-01T00:00:00Z"}
    async with session_factory() as session:
        failures = await validation.check(
            session=session,
            source="kap",
            table="ts.canonical_financial",
            record=record,
            rules=_TEST_RULES,
        )
        await session.commit()

    assert len(failures) == 1
    assert failures[0].rule_name == "test_missing_revenue"
    assert failures[0].failure_id is not None

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT source, rule_name, severity, record_table, record_pk, detail "
                    "FROM audit.validation_failure "
                    "WHERE failure_id = :fid"
                ),
                {"fid": failures[0].failure_id},
            )
        ).one()
    assert row.source == "kap"
    assert row.rule_name == "test_missing_revenue"
    assert row.severity == "warn"
    assert row.record_table == "ts.canonical_financial"
    # PK extraction now uses the full default registry signature for ts.canonical_financial:
    # (entity_id, period_end, as_of). period_end is missing from this record so it serializes
    # to None.
    assert row.record_pk == {
        "entity_id": 1,
        "period_end": None,
        "as_of": "2026-01-01T00:00:00Z",
    }
    assert row.detail == {"missing_field": "revenue"}


async def test_clean_record_writes_nothing(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    record = {"entity_id": 1, "revenue": 100, "as_of": "2026-01-01T00:00:00Z"}
    async with session_factory() as session:
        before = (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit.validation_failure "
                    "WHERE rule_name='test_missing_revenue'"
                )
            )
        ).scalar()
        failures = await validation.check(
            session=session,
            source="kap",
            table="ts.canonical_financial",
            record=record,
            rules=_TEST_RULES,
        )
        await session.commit()

    assert failures == []
    async with engine.connect() as conn:
        after = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit.validation_failure "
                    "WHERE rule_name='test_missing_revenue'"
                )
            )
        ).scalar()
    assert after == before


async def test_unknown_table_returns_empty_no_op(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A table with no rules registered must not raise; it is a no-op."""
    async with session_factory() as session:
        failures = await validation.check(
            session=session,
            source="kap",
            table="ts.never_registered",
            record={"x": 1},
            rules=_TEST_RULES,
        )
    assert failures == []
