"""Integration tests — bulk-write audit emission contract (codex F3 + F6 + F16).

Plan reference: v0.4.0 implementation plan Task 16.

ONE ``observation.write_batch`` event per ``write()`` call (NOT per
row). The event metadata carries bounded forensic detail
(``batch_size``, ``ts_min/max``, ``as_of_min/max``,
``batch_payload_hash``, counts, ``ingestion_run_id``). Per-key
forensic detail goes into ``audit.observation_batch_keys`` so the
high-cardinality detail does not bloat ``audit.events``.

Atomicity contract (codex F16, 2026-04-29): the per-key INSERT runs
in the SAME transaction as the audit event INSERT and the observation
INSERTs. If any of those fails the whole batch rolls back together.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
from aslan_core.errors import ObservationConflict
from aslan_core.schemas.timeseries import ObservationIn
from aslan_core.timeseries import ObservationWriter

pytestmark = pytest.mark.integration


async def _wipe(session: AsyncSession) -> None:
    for stmt in [
        "DELETE FROM audit.observation_batch_keys",
        "DELETE FROM ts.observation",
        "DELETE FROM ts.series_subject",
        "DELETE FROM ts.series_catalog",
        "DELETE FROM audit.events",
    ]:
        await session.execute(text(stmt))
    await session.commit()


@pytest.fixture(autouse=True)
async def _cleanup_ts(session: AsyncSession) -> AsyncIterator[None]:
    yield
    await session.rollback()
    await _wipe(session)


async def _seed(session: AsyncSession, job: str) -> int:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('kap', 'KAP', 'scraper', 'open') ON CONFLICT DO NOTHING"
        )
    )
    rid = await session.scalar(
        text(
            "INSERT INTO src.ingestion_run (source_id, job_name) "
            "VALUES ('kap', :job) RETURNING ingestion_run_id"
        ),
        {"job": job},
    )
    await session.commit()
    return int(rid)


@pytest.mark.asyncio(loop_scope="session")
async def test_write_emits_one_audit_event_per_call(session: AsyncSession) -> None:
    """ONE event per write() — not per row. The event metadata carries
    bounded forensic detail."""
    rid = await _seed(session, "audit-batch")
    set_actor(Actor(actor_id="user:abx", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    sr = await w.upsert_series(
        series_code="ab.1",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    obs = [
        ObservationIn(
            ts=datetime(2026, 1, d, tzinfo=UTC),
            as_of=datetime(2026, 1, d + 1, tzinfo=UTC),
            value=float(d),
        )
        for d in range(1, 11)
    ]
    await w.write(sr.series_id, obs)
    await session.commit()

    rows = (
        await session.execute(
            text(
                "SELECT operation, metadata FROM audit.events "
                "WHERE target_schema='ts' AND target_table='observation'"
            )
        )
    ).all()
    assert len(rows) == 1
    e = rows[0]
    assert e.operation == "observation.write_batch"
    md = e.metadata
    assert md["batch_size"] == 10
    assert md["inserted"] == 10
    assert md["unchanged"] == 0
    assert md["ts_min"] == "2026-01-01T00:00:00+00:00"
    assert md["ts_max"] == "2026-01-10T00:00:00+00:00"
    assert "batch_payload_hash" in md and len(md["batch_payload_hash"]) == 64


@pytest.mark.asyncio(loop_scope="session")
async def test_write_emits_per_key_batch_keys_rows(session: AsyncSession) -> None:
    """N observations → N rows in audit.observation_batch_keys with
    matching event_id + occurred_at (codex F3)."""
    rid = await _seed(session, "pk-rows")
    set_actor(Actor(actor_id="user:pk", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    sr = await w.upsert_series(
        series_code="ab.pk",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    obs = [
        ObservationIn(
            ts=datetime(2026, 2, d, tzinfo=UTC),
            as_of=datetime(2026, 2, d + 1, tzinfo=UTC),
            value=float(d),
        )
        for d in range(1, 4)
    ]
    await w.write(sr.series_id, obs)
    await session.commit()

    e = (
        await session.execute(
            text(
                "SELECT event_id, occurred_at, metadata FROM audit.events "
                "WHERE target_schema='ts' AND target_table='observation'"
            )
        )
    ).one()

    keys = (
        await session.execute(
            text(
                "SELECT series_id, ts, as_of, payload_hash, action "
                "FROM audit.observation_batch_keys "
                "WHERE event_id = :eid AND occurred_at = :oa "
                "ORDER BY ts"
            ),
            {"eid": e.event_id, "oa": e.occurred_at},
        )
    ).all()
    assert len(keys) == 3
    for k in keys:
        assert k.action == "inserted"
        assert k.series_id == sr.series_id


@pytest.mark.asyncio(loop_scope="session")
async def test_write_atomic_rollback_drops_audit_and_keys(
    session: AsyncSession,
) -> None:
    """Codex F3 + F6 — if the caller rolls back, both audit.events and
    audit.observation_batch_keys go too."""
    rid = await _seed(session, "rollback")
    set_actor(Actor(actor_id="user:rb", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    sr = await w.upsert_series(
        series_code="ab.rb",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    # First write — establishes a row.
    await w.write(
        sr.series_id,
        [
            ObservationIn(
                ts=datetime(2026, 3, 1, tzinfo=UTC),
                as_of=datetime(2026, 3, 2, tzinfo=UTC),
                value=1.0,
            ),
        ],
    )
    await session.commit()

    # Conflicting write — must roll back.
    with pytest.raises(ObservationConflict):
        await w.write(
            sr.series_id,
            [
                ObservationIn(
                    ts=datetime(2026, 3, 1, tzinfo=UTC),
                    as_of=datetime(2026, 3, 2, tzinfo=UTC),
                    value=999.0,
                ),
            ],
        )
    await session.rollback()

    # Exactly ONE event from the first successful call — none from the rolled-back one.
    n_events = await session.scalar(
        text(
            "SELECT COUNT(*) FROM audit.events "
            "WHERE target_schema='ts' AND target_table='observation'"
        )
    )
    assert n_events == 1
    n_keys = await session.scalar(text("SELECT COUNT(*) FROM audit.observation_batch_keys"))
    assert n_keys == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_write_atomic_rollback_when_batch_keys_insert_fails(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex F16, 2026-04-29: dangerous interleaving — audit.events INSERT
    succeeds, then the audit.observation_batch_keys INSERT raises. Both
    tables MUST roll back together. The previous test only exercised the
    case where ObservationConflict fired BEFORE the audit insert; this
    test forces the second-insert failure path that the conflict test
    can't reach.

    Implementation: monkeypatch ``session.execute`` to raise the next
    time an INSERT into ``audit.observation_batch_keys`` is issued.
    """
    rid = await _seed(session, "bk-fail")
    set_actor(Actor(actor_id="user:bk", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    sr = await w.upsert_series(
        series_code="ab.bk",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    # Snapshot table sizes before the doomed write.
    n_events_before = await session.scalar(
        text(
            "SELECT COUNT(*) FROM audit.events "
            "WHERE target_schema='ts' AND target_table='observation'"
        )
    )
    n_obs_before = await session.scalar(
        text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
        {"sid": sr.series_id},
    )

    original_execute = session.execute

    class BatchKeysBoom(RuntimeError):
        pass

    async def failing_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        # Trigger only on the per-key INSERT; let everything else through.
        sql = str(getattr(stmt, "text", stmt))
        if "INSERT INTO audit.observation_batch_keys" in sql:
            raise BatchKeysBoom("simulated batch_keys INSERT failure")
        return await original_execute(stmt, *args, **kwargs)

    monkeypatch.setattr(session, "execute", failing_execute)

    with pytest.raises(BatchKeysBoom):
        await w.write(
            sr.series_id,
            [
                ObservationIn(
                    ts=datetime(2026, 4, 1, tzinfo=UTC),
                    as_of=datetime(2026, 4, 2, tzinfo=UTC),
                    value=42.0,
                ),
            ],
        )
    await session.rollback()

    # The whole transaction unwound: no new audit.events row, no new
    # ts.observation row, no orphan batch_keys row.
    n_events_after = await session.scalar(
        text(
            "SELECT COUNT(*) FROM audit.events "
            "WHERE target_schema='ts' AND target_table='observation'"
        )
    )
    n_obs_after = await session.scalar(
        text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
        {"sid": sr.series_id},
    )
    assert n_events_after == n_events_before, (
        "audit.events did NOT roll back when batch_keys INSERT failed"
    )
    assert n_obs_after == n_obs_before, (
        "ts.observation did NOT roll back when batch_keys INSERT failed"
    )
    # No orphan key rows for this run_id.
    n_keys_for_run = await session.scalar(
        text("SELECT COUNT(*) FROM audit.observation_batch_keys WHERE series_id = :sid"),
        {"sid": sr.series_id},
    )
    assert n_keys_for_run == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_event_batch_size_matches_batch_keys_count(
    session: AsyncSession,
) -> None:
    """Codex F16, 2026-04-29: invariant — every ``observation.write_batch``
    audit event has ``metadata.batch_size`` rows in
    ``audit.observation_batch_keys`` keyed on (event_id, occurred_at).

    Run two writes of different sizes back-to-back, then assert per-event.
    """
    rid = await _seed(session, "inv")
    set_actor(Actor(actor_id="user:inv", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    sr = await w.upsert_series(
        series_code="ab.inv",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    # Write 3 rows.
    await w.write(
        sr.series_id,
        [
            ObservationIn(
                ts=datetime(2026, 5, d, tzinfo=UTC),
                as_of=datetime(2026, 5, d + 1, tzinfo=UTC),
                value=float(d),
            )
            for d in range(1, 4)
        ],
    )
    await session.commit()

    # Write 7 more rows.
    await w.write(
        sr.series_id,
        [
            ObservationIn(
                ts=datetime(2026, 6, d, tzinfo=UTC),
                as_of=datetime(2026, 6, d + 1, tzinfo=UTC),
                value=float(d),
            )
            for d in range(1, 8)
        ],
    )
    await session.commit()

    rows = (
        await session.execute(
            text(
                "SELECT event_id, occurred_at, metadata "
                "FROM audit.events "
                "WHERE target_schema='ts' AND target_table='observation' "
                "  AND ingestion_run_id = :rid "
                "ORDER BY occurred_at"
            ),
            {"rid": rid},
        )
    ).all()
    assert len(rows) == 2

    for r in rows:
        n_keys = await session.scalar(
            text(
                "SELECT COUNT(*) FROM audit.observation_batch_keys "
                "WHERE event_id = :eid AND occurred_at = :oa"
            ),
            {"eid": r.event_id, "oa": r.occurred_at},
        )
        assert n_keys == r.metadata["batch_size"], (
            f"event_id={r.event_id}: batch_size={r.metadata['batch_size']} "
            f"but {n_keys} batch_keys rows present"
        )
