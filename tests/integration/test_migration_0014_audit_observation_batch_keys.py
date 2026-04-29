"""Migration 0014 — audit.observation_batch_keys hypertable (codex F3).

Per-key forensic detail for bulk observation writes — keeps
``audit.events`` low-cardinality (one row per write_batch call) while
preserving full key-level traceability. Same ``chunk_time_interval=7d``
as ``audit.events`` so retention drops in lockstep (codex F6).

Codex Batch 1 F2: enforces parent-row presence via a deferred
constraint trigger and cascade-on-delete via a regular trigger on
``audit.events`` (Timescale rejects native cross-hypertable FKs:
"hypertables cannot be used as foreign key references of hypertables").
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_obkeys_table_and_hypertable(session: AsyncSession) -> None:
    n = await session.scalar(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='audit' AND table_name='observation_batch_keys'"
        )
    )
    assert n == 1
    n = await session.scalar(
        text(
            "SELECT COUNT(*) FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema='audit' "
            "AND hypertable_name='observation_batch_keys'"
        )
    )
    assert n == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_obkeys_chunk_interval_matches_events(session: AsyncSession) -> None:
    """Codex F6 — both audit.events and audit.observation_batch_keys
    use the same chunk_time_interval (7d) so retention drops in
    lockstep."""
    events_iv = await session.scalar(
        text(
            "SELECT time_interval FROM timescaledb_information.dimensions "
            "WHERE hypertable_schema='audit' AND hypertable_name='events'"
        )
    )
    obkeys_iv = await session.scalar(
        text(
            "SELECT time_interval FROM timescaledb_information.dimensions "
            "WHERE hypertable_schema='audit' AND hypertable_name='observation_batch_keys'"
        )
    )
    assert events_iv == obkeys_iv


@pytest.mark.asyncio(loop_scope="session")
async def test_obkeys_pk(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'audit.observation_batch_keys'::regclass "
            "AND i.indisprimary "
            "ORDER BY array_position(i.indkey, a.attnum)"
        )
    )
    pk_cols = [r.attname for r in rows]
    assert pk_cols == ["event_id", "occurred_at", "series_id", "ts", "as_of"]


async def _insert_parent_event(
    session: AsyncSession,
    *,
    actor_id: str = "user:pytest",
    operation: str = "test_op",
) -> tuple[int, object]:
    """Insert a parent audit.events row and return (event_id, occurred_at).

    Used by F2 trigger tests so the deferred parent-check constraint
    trigger has something real to point at.
    """
    row = (
        await session.execute(
            text(
                "INSERT INTO audit.events "
                "(actor_id, actor_kind, operation, target_schema, target_table, target_pk) "
                "VALUES (:aid, 'user', :op, 'ts', 'observation', '{}'::jsonb) "
                "RETURNING event_id, occurred_at"
            ),
            {"aid": actor_id, "op": operation},
        )
    ).one()
    await session.commit()
    return int(row.event_id), row.occurred_at


@pytest.mark.asyncio(loop_scope="session")
async def test_action_check_rejects_unknown(session: AsyncSession) -> None:
    """The action CHECK rejects 'updated' synchronously at INSERT time
    (before the deferred parent-check trigger fires at COMMIT)."""
    eid, oat = await _insert_parent_event(session, operation="action_check")
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO audit.observation_batch_keys "
                "(event_id, occurred_at, series_id, ts, as_of, payload_hash, action) "
                "VALUES (:eid, :oat, 1, now(), now(), repeat('a', 64), 'updated')"
            ),
            {"eid": eid, "oat": oat},
        )
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_payload_hash_must_be_64_chars(session: AsyncSession) -> None:
    """payload_hash is a CHAR(64) — short or long values are rejected."""
    eid, oat = await _insert_parent_event(session, operation="hash_len_check")
    with pytest.raises(Exception):  # noqa: B017 -- CHAR(N) raises StringDataRightTruncation
        await session.execute(
            text(
                "INSERT INTO audit.observation_batch_keys "
                "(event_id, occurred_at, series_id, ts, as_of, payload_hash, action) "
                "VALUES (:eid, :oat, 1, now(), now(), 'short', 'inserted')"
            ),
            {"eid": eid, "oat": oat},
        )
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_obkeys_event_index_present(session: AsyncSession) -> None:
    rows = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='audit' AND tablename='observation_batch_keys'"
        )
    )
    names = {r.indexname for r in rows}
    assert "obkeys_event" in names


@pytest.mark.asyncio(loop_scope="session")
async def test_obkeys_inserted_action_is_accepted(session: AsyncSession) -> None:
    """Sanity — the 'inserted' and 'unchanged' actions are accepted
    when paired with a real parent audit.events row."""
    eid, oat = await _insert_parent_event(session, operation="happy_path")
    await session.execute(
        text(
            "INSERT INTO audit.observation_batch_keys "
            "(event_id, occurred_at, series_id, ts, as_of, payload_hash, action) "
            "VALUES (:eid, :oat, 1, now(), now(), repeat('a', 64), 'inserted'), "
            "       (:eid, :oat, 2, now(), now(), repeat('b', 64), 'unchanged')"
        ),
        {"eid": eid, "oat": oat},
    )
    await session.commit()
    # Cleanup — DELETE the parent event; the cascade trigger removes the keys.
    await session.execute(
        text("DELETE FROM audit.events WHERE event_id = :eid"),
        {"eid": eid},
    )
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_batch_keys_orphan_rejected(session: AsyncSession) -> None:
    """Codex Batch 1 F2 — INSERT into observation_batch_keys with an
    event_id/occurred_at that doesn't exist in audit.events must fail
    at COMMIT (the constraint trigger is DEFERRABLE INITIALLY DEFERRED,
    so the violation surfaces when the deferred queue runs at commit
    time, not at INSERT)."""
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO audit.observation_batch_keys "
                "(event_id, occurred_at, series_id, ts, as_of, payload_hash, action) "
                "VALUES (9999999, now(), 1, now(), now(), repeat('a', 64), 'inserted')"
            )
        )
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_batch_keys_mismatched_occurred_at_rejected(session: AsyncSession) -> None:
    """Codex Batch 1 F2 — a row with a valid event_id but the wrong
    occurred_at is also rejected (the FK shape is composite
    ``(event_id, occurred_at)``, not just ``event_id``)."""
    eid, _oat = await _insert_parent_event(session, operation="mismatch_oat")
    with pytest.raises(IntegrityError):
        # Use an occurred_at deliberately offset by 1 second from the
        # parent's, so the (event_id, occurred_at) tuple doesn't exist.
        await session.execute(
            text(
                "INSERT INTO audit.observation_batch_keys "
                "(event_id, occurred_at, series_id, ts, as_of, payload_hash, action) "
                "VALUES (:eid, now() - INTERVAL '1 hour', 1, now(), now(), "
                "        repeat('a', 64), 'inserted')"
            ),
            {"eid": eid},
        )
        await session.commit()
    await session.rollback()
    # Clean up the parent event since the rolled-back batch_keys insert
    # leaves audit.events behind.
    await session.execute(
        text("DELETE FROM audit.events WHERE event_id = :eid"),
        {"eid": eid},
    )
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_batch_keys_cascade_deletes_on_event_delete(session: AsyncSession) -> None:
    """Codex Batch 1 F2 — DELETE on audit.events cascades to
    audit.observation_batch_keys via the events_cascade_keys trigger
    (Timescale rejects native ON DELETE CASCADE FKs across hypertables)."""
    eid, oat = await _insert_parent_event(session, operation="cascade_delete")
    await session.execute(
        text(
            "INSERT INTO audit.observation_batch_keys "
            "(event_id, occurred_at, series_id, ts, as_of, payload_hash, action) "
            "VALUES (:eid, :oat, 1, now(), now(), repeat('a', 64), 'inserted')"
        ),
        {"eid": eid, "oat": oat},
    )
    await session.commit()

    n_before = await session.scalar(
        text("SELECT COUNT(*) FROM audit.observation_batch_keys WHERE event_id = :eid"),
        {"eid": eid},
    )
    assert n_before == 1

    await session.execute(
        text("DELETE FROM audit.events WHERE event_id = :eid"),
        {"eid": eid},
    )
    await session.commit()

    n_after = await session.scalar(
        text("SELECT COUNT(*) FROM audit.observation_batch_keys WHERE event_id = :eid"),
        {"eid": eid},
    )
    assert n_after == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_batch_keys_rejects_non_hex_payload_hash(session: AsyncSession) -> None:
    """Codex Batch 1 F3 — payload_hash must match ``^[0-9a-f]{64}$``.
    64 'z' chars is the right length but not hex, so the storage
    boundary rejects it (defense-in-depth on top of CHAR(64)+length
    CHECK)."""
    eid, oat = await _insert_parent_event(session, operation="hash_nonhex")
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO audit.observation_batch_keys "
                "(event_id, occurred_at, series_id, ts, as_of, payload_hash, action) "
                "VALUES (:eid, :oat, 1, now(), now(), repeat('z', 64), 'inserted')"
            ),
            {"eid": eid, "oat": oat},
        )
        await session.commit()
    await session.rollback()
    await session.execute(
        text("DELETE FROM audit.events WHERE event_id = :eid"),
        {"eid": eid},
    )
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_batch_keys_rejects_uppercase_payload_hash(session: AsyncSession) -> None:
    """Codex Batch 1 F3 — uppercase hex is also rejected;
    hashlib.sha256().hexdigest() always returns lowercase."""
    eid, oat = await _insert_parent_event(session, operation="hash_upper")
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO audit.observation_batch_keys "
                "(event_id, occurred_at, series_id, ts, as_of, payload_hash, action) "
                "VALUES (:eid, :oat, 1, now(), now(), repeat('A', 64), 'inserted')"
            ),
            {"eid": eid, "oat": oat},
        )
        await session.commit()
    await session.rollback()
    await session.execute(
        text("DELETE FROM audit.events WHERE event_id = :eid"),
        {"eid": eid},
    )
    await session.commit()
