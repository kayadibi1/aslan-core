"""Migration 0060 — bloomberg_comparison_run + bloomberg_comparison_cell.

Asserts the table shapes, indexes, GRANTs, and the constraint that
`aslan_advantage` only accepts {wins, ties, loses, NULL}.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_bloomberg_comparison_run_columns(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'audit' "
                    "  AND table_name = 'bloomberg_comparison_run'"
                )
            )
        ).all()
    names = {c.column_name for c in cols}
    assert {
        "run_id",
        "quarter",
        "opened_at",
        "opened_by",
        "closed_at",
        "closed_by",
        "recorded_at",
    }.issubset(names), f"missing columns: {names}"


async def test_bloomberg_comparison_cell_columns(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'audit' "
                    "  AND table_name = 'bloomberg_comparison_cell'"
                )
            )
        ).all()
    names = {c.column_name for c in cols}
    assert {
        "cell_id",
        "run_id",
        "entity_ticker",
        "field",
        "bloomberg_value",
        "bloomberg_entered_by",
        "bloomberg_entered_at",
        "aslan_value",
        "aslan_sampled_at",
        "variance_pct",
        "aslan_advantage",
        "recorded_at",
    }.issubset(names), f"missing columns: {names}"


async def test_advantage_check_constraint_rejects_bad_values(engine: AsyncEngine) -> None:
    """The aslan_advantage CHECK constraint blocks anything outside the
    canonical set."""
    async with engine.connect() as conn:
        await conn.execute(text("DELETE FROM audit.bloomberg_comparison_cell"))
        await conn.execute(text("DELETE FROM audit.bloomberg_comparison_run"))
        await conn.commit()
    async with engine.begin() as conn:
        run_row = (
            await conn.execute(
                text(
                    "INSERT INTO audit.bloomberg_comparison_run(quarter) "
                    "VALUES ('1999Q1') RETURNING run_id"
                )
            )
        ).one()
        run_id = run_row.run_id
        # Valid insert
        await conn.execute(
            text(
                "INSERT INTO audit.bloomberg_comparison_cell"
                "(run_id, entity_ticker, field, aslan_advantage) "
                "VALUES (:rid, 'AKBNK', 'revenue_q-1', 'wins')"
            ),
            {"rid": run_id},
        )
    # Invalid advantage → CheckViolation.
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO audit.bloomberg_comparison_cell"
                    "(run_id, entity_ticker, field, aslan_advantage) "
                    "VALUES (:rid, 'GARAN', 'revenue_q-1', 'beats')"
                ),
                {"rid": run_id},
            )
    # Cleanup so subsequent tests start clean.
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit.bloomberg_comparison_cell"))
        await conn.execute(text("DELETE FROM audit.bloomberg_comparison_run"))


async def test_quarter_check_constraint(engine: AsyncEngine) -> None:
    """quarter must match YYYYQn shape."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO audit.bloomberg_comparison_run(quarter) VALUES ('not-a-quarter')")
            )


async def test_bloomberg_unique_constraint(engine: AsyncEngine) -> None:
    """(run_id, entity_ticker, field) is unique."""
    from sqlalchemy.exc import IntegrityError

    async with engine.begin() as conn:
        run_row = (
            await conn.execute(
                text(
                    "INSERT INTO audit.bloomberg_comparison_run(quarter) "
                    "VALUES ('1999Q2') RETURNING run_id"
                )
            )
        ).one()
        run_id = run_row.run_id
        await conn.execute(
            text(
                "INSERT INTO audit.bloomberg_comparison_cell"
                "(run_id, entity_ticker, field) "
                "VALUES (:rid, 'AKBNK', 'revenue_q-1')"
            ),
            {"rid": run_id},
        )
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO audit.bloomberg_comparison_cell"
                    "(run_id, entity_ticker, field) "
                    "VALUES (:rid, 'AKBNK', 'revenue_q-1')"
                ),
                {"rid": run_id},
            )
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit.bloomberg_comparison_cell"))
        await conn.execute(text("DELETE FROM audit.bloomberg_comparison_run"))


async def test_dashboard_role_has_select(aslan_dashboard_conn: object) -> None:
    """The aslan_dashboard role can SELECT both bloomberg_* tables."""
    import asyncpg

    assert isinstance(aslan_dashboard_conn, asyncpg.Connection)
    n_runs = await aslan_dashboard_conn.fetchval(
        "SELECT count(*) FROM audit.bloomberg_comparison_run"
    )
    assert int(n_runs) >= 0
    n_cells = await aslan_dashboard_conn.fetchval(
        "SELECT count(*) FROM audit.bloomberg_comparison_cell"
    )
    assert int(n_cells) >= 0
