"""Integration test: migration 0010 — audit schema + events hypertable.

Confirms the audit schema, the hypertable registration, the four
indexes, and the actor_kind CHECK constraint on audit.events.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_schema_exists(session: AsyncSession) -> None:
    n = await session.scalar(
        text("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = 'audit'")
    )
    assert n == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_events_table_exists(session: AsyncSession) -> None:
    n = await session.scalar(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'audit' AND table_name = 'events'"
        )
    )
    assert n == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_events_is_hypertable(session: AsyncSession) -> None:
    n = await session.scalar(
        text(
            "SELECT COUNT(*) FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema = 'audit' AND hypertable_name = 'events'"
        )
    )
    assert n == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_events_indexes_present(session: AsyncSession) -> None:
    rows = await session.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'audit' AND tablename = 'events'")
    )
    names = {r.indexname for r in rows}
    for expected in (
        "events_actor_time",
        "events_target",
        "events_request_id",
        "events_run",
    ):
        assert expected in names, f"missing index {expected!r}; got {names}"


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_events_partial_indexes_have_predicate(
    session: AsyncSession,
) -> None:
    """events_request_id and events_run are partial indexes
    (... WHERE col IS NOT NULL). Confirm the predicate landed."""
    rows = await session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'audit' AND tablename = 'events' "
            "  AND indexname IN ('events_request_id', 'events_run')"
        )
    )
    by_name = {r.indexname: r.indexdef for r in rows}
    assert "IS NOT NULL" in by_name["events_request_id"]
    assert "IS NOT NULL" in by_name["events_run"]


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_events_actor_kind_check_constraint(
    session: AsyncSession,
) -> None:
    """An invalid actor_kind ('alien') must fail at the CHECK constraint,
    not silently land in the audit log."""
    sp = await session.begin_nested()
    try:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO audit.events "
                    "  (actor_id, actor_kind, operation, "
                    "   target_schema, target_table, target_pk) "
                    "VALUES ('user:x', 'alien', 'op', 's', 't', '{}'::jsonb)"
                )
            )
    finally:
        # The CHECK violation aborts the savepoint; explicitly roll it
        # back so the outer session/transaction stays usable.
        if sp.is_active:
            await sp.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_events_primary_key_is_event_id_plus_occurred_at(
    session: AsyncSession,
) -> None:
    """Timescale requires the chunking column to be part of the PK."""
    rows = await session.execute(
        text(
            "SELECT a.attname AS col "
            "FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid "
            "  AND a.attnum = ANY(i.indkey) "
            "WHERE n.nspname = 'audit' AND c.relname = 'events' "
            "  AND i.indisprimary"
        )
    )
    pk_cols = {r.col for r in rows}
    assert pk_cols == {"event_id", "occurred_at"}
