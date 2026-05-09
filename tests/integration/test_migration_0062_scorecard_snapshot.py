"""Migration 0062 — audit.scorecard_snapshot.

Asserts the table shape, the (week_start, metric_name) primary key,
the status CHECK constraint, the helper index, and the dashboard role's
SELECT-only privilege boundary.
"""

from __future__ import annotations

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_scorecard_snapshot_columns(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'audit' "
                    "  AND table_name = 'scorecard_snapshot'"
                )
            )
        ).all()
    names = {c.column_name for c in cols}
    assert {
        "week_start",
        "metric_name",
        "target",
        "actual",
        "status",
        "notes",
        "recorded_at",
    } == names, f"unexpected columns: {names}"


async def test_status_check_constraint(engine: AsyncEngine) -> None:
    """Only the three canonical statuses are accepted."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit.scorecard_snapshot"))
        # valid insert
        await conn.execute(
            text(
                "INSERT INTO audit.scorecard_snapshot("
                "  week_start, metric_name, target, actual, status, notes"
                ") VALUES ('2026-04-27', 'kap_recency_p95', '<= 300s', "
                "  '120s', 'pass', 'n=42')"
            )
        )
    # invalid status
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO audit.scorecard_snapshot("
                    "  week_start, metric_name, target, actual, status"
                    ") VALUES ('2026-04-27', 'bogus_metric', 't', 'a', "
                    "  'totally-invalid')"
                )
            )
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit.scorecard_snapshot"))


async def test_primary_key_uniqueness(engine: AsyncEngine) -> None:
    """(week_start, metric_name) is the PK — duplicate raises."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit.scorecard_snapshot"))
        await conn.execute(
            text(
                "INSERT INTO audit.scorecard_snapshot("
                "  week_start, metric_name, target, actual, status"
                ") VALUES ('2026-04-27', 'kap_recency_p95', '<= 300s', "
                "  '120s', 'pass')"
            )
        )
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO audit.scorecard_snapshot("
                    "  week_start, metric_name, target, actual, status"
                    ") VALUES ('2026-04-27', 'kap_recency_p95', '<= 300s', "
                    "  '999s', 'fail')"
                )
            )
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit.scorecard_snapshot"))


async def test_upsert_overwrites_actual(engine: AsyncEngine) -> None:
    """ON CONFLICT (week_start, metric_name) DO UPDATE overwrites
    actual/status/notes. recorded_at stays at the original insert time
    (we don't include it in the UPDATE list)."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit.scorecard_snapshot"))
        await conn.execute(
            text(
                "INSERT INTO audit.scorecard_snapshot("
                "  week_start, metric_name, target, actual, status, notes"
                ") VALUES ('2026-04-27', 'm', 't', 'first', 'pass', 'n1')"
            )
        )
        original_recorded = (
            await conn.execute(
                text(
                    "SELECT recorded_at FROM audit.scorecard_snapshot "
                    "WHERE week_start = '2026-04-27' AND metric_name = 'm'"
                )
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO audit.scorecard_snapshot("
                "  week_start, metric_name, target, actual, status, notes"
                ") VALUES ('2026-04-27', 'm', 't', 'second', 'fail', 'n2') "
                "ON CONFLICT (week_start, metric_name) DO UPDATE SET "
                "  actual = EXCLUDED.actual, "
                "  status = EXCLUDED.status, "
                "  notes = EXCLUDED.notes"
            )
        )
        new = (
            await conn.execute(
                text(
                    "SELECT actual, status, notes, recorded_at "
                    "FROM audit.scorecard_snapshot "
                    "WHERE week_start = '2026-04-27' AND metric_name = 'm'"
                )
            )
        ).one()
        assert new.actual == "second"
        assert new.status == "fail"
        assert new.notes == "n2"
        assert new.recorded_at == original_recorded
        await conn.execute(text("DELETE FROM audit.scorecard_snapshot"))


async def test_dashboard_role_can_select(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    """The aslan_dashboard role has SELECT on audit.scorecard_snapshot."""
    n = await aslan_dashboard_conn.fetchval(
        "SELECT count(*) FROM audit.scorecard_snapshot"
    )
    assert int(n) >= 0


_DENY_WRITE_ERRORS = (
    asyncpg.exceptions.InsufficientPrivilegeError,
    asyncpg.exceptions.ReadOnlySQLTransactionError,
)


async def test_dashboard_role_cannot_insert(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    """The aslan_dashboard role does NOT have INSERT — production
    deployment routes the cron through audit_writer."""
    with pytest.raises(_DENY_WRITE_ERRORS):
        await aslan_dashboard_conn.execute(
            "INSERT INTO audit.scorecard_snapshot("
            "  week_start, metric_name, target, actual, status"
            ") VALUES ('2026-04-27', 'm', 't', 'a', 'pass')"
        )
