"""SECURITY DEFINER helper composes correctly with the column-allowlist.

Spec §8.2 + codex round-9: the dashboard role can EXECUTE
``audit.event_metadata_key_count(BIGINT, TIMESTAMPTZ)`` to render
``metadata_key_count`` in the audit page WITHOUT obtaining SELECT on
``audit.events.metadata``. The contract:

(a) Calling the helper returns the correct count.
(b) Selecting the raw ``metadata`` column raises 42501.
(c) ``SELECT *`` on ``audit.events`` raises 42501 (covers ``before``,
    ``after``, ``metadata``).
(d) Mixing the helper output with the forbidden column in one
    projection still raises 42501 — the helper cannot be combined with
    the forbidden column to leak the bytes.
(e) Codex round-9: ``pg_proc.proowner = aslan_app`` so SECURITY DEFINER
    runs as the role that holds SELECT on ``metadata``, not as whatever
    role applied the migration.

Renamed from the plan's ``test_dashboard_outbox_payload_size_helper.py``:
the outbox-size helper was dropped during plan-rounds in favour of
``audit.event_metadata_key_count``. The compositional contract is
identical for either helper; this file tests the one that actually
shipped in migration 0020.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(loop_scope="session")
async def _seed_audit_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[int, str]]:
    """Insert one ``audit.events`` row with a 3-key metadata blob.
    Returns (event_id, occurred_at_iso) so tests can call the helper
    with the matching composite PK."""
    # JSON literals with ``:digit`` substrings (e.g. ``{"x":1}``) collide
    # with SQLAlchemy ``text()``'s bind-parameter scanner — bind through
    # parameters instead of inlining. ``CAST(:bind AS jsonb)`` is used
    # rather than ``:bind::jsonb`` because SQLAlchemy's regex does not
    # recognise a bind whose word is immediately followed by ``::``
    # (the cast colons satisfy the negative lookahead and the bind is
    # passed through verbatim, producing a syntax error).
    async with session_factory() as s:
        result = await s.execute(
            text(
                "INSERT INTO audit.events "
                "(actor_id, actor_kind, operation, target_schema, target_table, "
                " target_pk, metadata) "
                "VALUES ('user-helper-test', 'user', 'insert', 'audit', 'events', "
                "        CAST(:pk_json AS jsonb), CAST(:md_json AS jsonb)) "
                "RETURNING event_id, occurred_at"
            ),
            {
                "pk_json": '{"x":1}',
                "md_json": '{"k1":"v1","k2":"v2","k3":"v3"}',
            },
        )
        ev_id, ev_ts = result.one()
        await s.commit()

    yield (ev_id, ev_ts.isoformat())

    async with session_factory() as s:
        await s.execute(
            text("DELETE FROM audit.events WHERE event_id = :eid AND occurred_at = :ts"),
            {"eid": ev_id, "ts": ev_ts},
        )
        await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_helper_returns_metadata_key_count(
    aslan_dashboard_conn: asyncpg.Connection,
    _seed_audit_event: tuple[int, str],
) -> None:
    ev_id, _ev_ts_iso = _seed_audit_event
    count = await aslan_dashboard_conn.fetchval(
        "SELECT audit.event_metadata_key_count($1, "
        "  (SELECT occurred_at FROM audit.events WHERE event_id = $1))",
        ev_id,
    )
    assert count == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_select_metadata_column_raises_insufficient_privilege(
    aslan_dashboard_conn: asyncpg.Connection,
    _seed_audit_event: tuple[int, str],
) -> None:
    """The helper exists but the raw column is still unreachable."""
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await aslan_dashboard_conn.fetch("SELECT metadata FROM audit.events LIMIT 1")


@pytest.mark.asyncio(loop_scope="session")
async def test_select_star_audit_events_raises_insufficient_privilege(
    aslan_dashboard_conn: asyncpg.Connection,
    _seed_audit_event: tuple[int, str],
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await aslan_dashboard_conn.fetch("SELECT * FROM audit.events LIMIT 1")


@pytest.mark.asyncio(loop_scope="session")
async def test_helper_combined_with_metadata_column_raises_insufficient_privilege(
    aslan_dashboard_conn: asyncpg.Connection,
    _seed_audit_event: tuple[int, str],
) -> None:
    """The helper cannot be combined in the same projection with the
    forbidden column. Even though the helper alone is allowed, mixing
    it with ``metadata`` in one SELECT triggers the column-allowlist
    check on ``metadata`` first → 42501."""
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await aslan_dashboard_conn.fetch(
            "SELECT audit.event_metadata_key_count(event_id, occurred_at), metadata "
            "FROM audit.events LIMIT 1"
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_helper_owner_is_aslan_app(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    """Codex round-9: the SECURITY DEFINER must be owned by ``aslan_app``
    (which holds SELECT on ``metadata``), not by whatever role applied
    the migration."""
    owner: str = await aslan_dashboard_conn.fetchval(
        "SELECT r.rolname "
        "FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "JOIN pg_roles r ON r.oid = p.proowner "
        "WHERE n.nspname = 'audit' AND p.proname = 'event_metadata_key_count'"
    )
    assert owner == "aslan_app"
