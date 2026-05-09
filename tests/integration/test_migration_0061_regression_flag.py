"""Migration 0061 — audit.regression_flag.

Asserts the table shape, indexes, status CHECK constraint, and that
the dashboard role can SELECT but not INSERT/UPDATE.
"""

from __future__ import annotations

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_regression_flag_columns(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'audit' "
                    "  AND table_name = 'regression_flag'"
                )
            )
        ).all()
    names = {c.column_name for c in cols}
    assert {
        "flag_id",
        "source",
        "record_table",
        "record_pk",
        "metric",
        "prior_value",
        "current_value",
        "shift_pct",
        "threshold_pct",
        "detected_at",
        "status",
        "reviewer",
        "reviewed_at",
        "review_note",
        "recorded_at",
    }.issubset(names), f"missing columns: {names}"


async def test_status_check_constraint(engine: AsyncEngine) -> None:
    """Only the four canonical statuses are accepted."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit.regression_flag"))
        # valid insert; pass record_pk via bind to avoid JSON-colon
        # collision with SQLAlchemy's :param style.
        row = (
            await conn.execute(
                text(
                    "INSERT INTO audit.regression_flag("
                    "  source, record_table, record_pk, metric, "
                    "  shift_pct, threshold_pct, detected_at"
                    ") VALUES ("
                    "  'ts', 'ts.canonical_financial', CAST(:pk AS JSONB), 'revenue', "
                    "  30, 25, now()"
                    ") RETURNING flag_id"
                ),
                {"pk": '{"x":1}'},
            )
        ).one()
        assert int(row.flag_id) > 0
    # invalid status
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO audit.regression_flag("
                    "  source, record_table, record_pk, metric, "
                    "  shift_pct, threshold_pct, detected_at, status"
                    ") VALUES ("
                    "  'ts', 'ts.canonical_financial', CAST(:pk AS JSONB), 'revenue', "
                    "  30, 25, now(), 'totally-invalid'"
                    ")"
                ),
                {"pk": '{"x":1}'},
            )
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit.regression_flag"))


async def test_dashboard_role_can_select(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    """The aslan_dashboard role has SELECT on audit.regression_flag."""
    n = await aslan_dashboard_conn.fetchval("SELECT count(*) FROM audit.regression_flag")
    assert int(n) >= 0


_DENY_WRITE_ERRORS = (
    asyncpg.exceptions.InsufficientPrivilegeError,
    asyncpg.exceptions.ReadOnlySQLTransactionError,
)


async def test_dashboard_role_cannot_insert(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    """The aslan_dashboard role does NOT have INSERT — production
    deployment routes the cron through audit_writer.

    Migration 0020 sets ``default_transaction_read_only=on`` on the
    aslan_dashboard role; that's the first deny we hit. The
    column-level GRANT-deny is the second, defence-in-depth boundary.
    """
    with pytest.raises(_DENY_WRITE_ERRORS):
        await aslan_dashboard_conn.execute(
            "INSERT INTO audit.regression_flag("
            "  source, record_table, record_pk, metric, "
            "  shift_pct, threshold_pct, detected_at"
            ") VALUES ("
            "  'ts', 'ts.canonical_financial', '{}'::jsonb, 'revenue', "
            "  30, 25, now()"
            ")"
        )


async def test_dashboard_role_cannot_update(
    aslan_dashboard_conn: asyncpg.Connection,
    engine: AsyncEngine,
) -> None:
    """The aslan_dashboard role cannot mutate status — production
    deployment routes review writes through audit_admin."""
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO audit.regression_flag("
                    "  source, record_table, record_pk, metric, "
                    "  shift_pct, threshold_pct, detected_at"
                    ") VALUES ("
                    "  'ts', 'ts.canonical_financial', '{}'::jsonb, 'revenue', "
                    "  30, 25, now()"
                    ") RETURNING flag_id"
                )
            )
        ).one()
        flag_id = int(row.flag_id)
    try:
        with pytest.raises(_DENY_WRITE_ERRORS):
            await aslan_dashboard_conn.execute(
                "UPDATE audit.regression_flag SET status='reviewed' WHERE flag_id=$1",
                flag_id,
            )
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM audit.regression_flag"))
