"""Integration tests — ``ObservationWriter.write`` Phase 0/1/2 contract.

Plan reference: v0.4.0 implementation plan Task 14. Covers the
in-memory dedup + pre-flight validation contract (codex F5 + F25),
the DB-side conflict detection contract (codex F2), and the
zero-DB-I/O Phase 0 spy contract (codex F14).
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


async def _seed_sources(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('kap', 'KAP', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING"
        )
    )
    await session.commit()


async def _new_run(session: AsyncSession) -> int:
    await _seed_sources(session)
    rid = await session.scalar(
        text(
            "INSERT INTO src.ingestion_run (source_id, job_name) "
            "VALUES ('kap', 'write-basic') RETURNING ingestion_run_id"
        )
    )
    await session.commit()
    return int(rid)


async def _seed_series(session: AsyncSession, rid: int, code: str = "wb.1") -> int:
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    r = await w.upsert_series(
        series_code=code,
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()
    return r.series_id


def _ts(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


@pytest.mark.asyncio(loop_scope="session")
async def test_write_inserts_rows(session: AsyncSession) -> None:
    rid = await _new_run(session)
    sid = await _seed_series(session, rid)
    w = ObservationWriter(session, ingestion_run_id=rid)

    obs = [ObservationIn(ts=_ts(d), as_of=_ts(d + 1), value=float(d)) for d in range(1, 6)]
    wc = await w.write(sid, obs)
    await session.commit()

    assert wc.attempted == 5
    assert wc.inserted == 5
    assert wc.unchanged == 0
    assert wc.updated == 0

    n = await session.scalar(
        text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
        {"sid": sid},
    )
    assert n == 5


@pytest.mark.asyncio(loop_scope="session")
async def test_write_identical_retry_counts_unchanged(session: AsyncSession) -> None:
    """Retrying the same batch with identical payloads → unchanged, no error."""
    rid = await _new_run(session)
    sid = await _seed_series(session, rid, code="wb.idem")
    w = ObservationWriter(session, ingestion_run_id=rid)

    obs = [ObservationIn(ts=_ts(1), as_of=_ts(2), value=1.0)]
    wc1 = await w.write(sid, obs)
    await session.commit()
    assert wc1.inserted == 1 and wc1.unchanged == 0

    wc2 = await w.write(sid, obs)
    await session.commit()
    assert wc2.attempted == 1
    assert wc2.inserted == 0
    assert wc2.unchanged == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_write_same_key_different_db_payload_raises(
    session: AsyncSession,
) -> None:
    """Codex F2 — DB row exists with payload A; batch contains key with
    payload B → raise ObservationConflict; rollback; no partial writes."""
    rid = await _new_run(session)
    sid = await _seed_series(session, rid, code="wb.conflict")
    w = ObservationWriter(session, ingestion_run_id=rid)

    obs_a = [ObservationIn(ts=_ts(1), as_of=_ts(2), value=1.0)]
    await w.write(sid, obs_a)
    await session.commit()

    obs_b = [
        ObservationIn(ts=_ts(1), as_of=_ts(2), value=999.0),  # SAME key, DIFFERENT value
        ObservationIn(ts=_ts(2), as_of=_ts(3), value=2.0),  # would-be valid
    ]
    with pytest.raises(ObservationConflict, match="DB row exists"):
        await w.write(sid, obs_b)

    await session.rollback()

    # Only the ts=_ts(1) row from the original write must exist; the
    # second ts=_ts(2) row from the failed batch must NOT be present.
    n = await session.scalar(
        text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
        {"sid": sid},
    )
    assert n == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_write_intra_batch_same_key_different_payload_raises_before_io(
    session: AsyncSession,
) -> None:
    """Codex F5 — intra-batch dedup runs in Python BEFORE any DB I/O.
    Two rows with same (series_id, ts, as_of) but different values must
    raise ObservationConflict; the DB must be untouched."""
    rid = await _new_run(session)
    sid = await _seed_series(session, rid, code="wb.intra")
    w = ObservationWriter(session, ingestion_run_id=rid)

    obs = [
        ObservationIn(ts=_ts(1), as_of=_ts(2), value=1.0),
        ObservationIn(ts=_ts(1), as_of=_ts(2), value=2.0),  # same key, different value
    ]
    with pytest.raises(ObservationConflict, match="intra-batch"):
        await w.write(sid, obs)

    n = await session.scalar(
        text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
        {"sid": sid},
    )
    assert n == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_phase_0_intra_batch_conflict_raises_before_any_db_io(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex F14, 2026-04-29: Phase 0 dedup must raise BEFORE any
    DB I/O. We spy on AsyncSession.execute and assert it's never
    called when the batch contains divergent same-key duplicates.

    This catches the failure mode where an implementer reorders the
    body of write() so the advisory lock acquisition (Phase 1) or a
    source_id lookup runs *before* the in-memory dedup. Such an
    implementation would still pass the row-count assertion in
    test_write_intra_batch_same_key_different_payload_raises_before_io
    because the failed transaction would roll back its inserts — but
    the contract says the writer must not even open a server-side
    transaction or take a lock when the batch is structurally
    invalid. The spy guarantees that.
    """
    rid = await _new_run(session)
    sid = await _seed_series(session, rid, code="wb.phase0.spy")
    w = ObservationWriter(session, ingestion_run_id=rid)
    await session.commit()

    execute_calls: list[Any] = []
    original_execute = session.execute

    async def spy_execute(*args: Any, **kwargs: Any) -> Any:
        execute_calls.append(args)
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(session, "execute", spy_execute)

    # Two observations, same key, different value — must raise in Phase 0.
    obs = [
        ObservationIn(ts=_ts(1), as_of=_ts(2), value=1.0),
        ObservationIn(ts=_ts(1), as_of=_ts(2), value=2.0),
    ]
    with pytest.raises(ObservationConflict, match="intra-batch"):
        await w.write(sid, obs)

    # Zero DB I/O happened — Phase 0 returned BEFORE any of:
    # advisory lock acquisition, source_id lookup, INSERT, audit emission.
    assert len(execute_calls) == 0, (
        f"Expected 0 execute calls before Phase 0 raise, got {len(execute_calls)}: {execute_calls}"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_write_intra_batch_identical_duplicate_silently_dropped(
    session: AsyncSession,
) -> None:
    """Two rows with identical key + payload → keep one, drop the other."""
    rid = await _new_run(session)
    sid = await _seed_series(session, rid, code="wb.dupe")
    w = ObservationWriter(session, ingestion_run_id=rid)

    obs = [
        ObservationIn(ts=_ts(1), as_of=_ts(2), value=1.0),
        ObservationIn(ts=_ts(1), as_of=_ts(2), value=1.0),  # identical
    ]
    wc = await w.write(sid, obs)
    await session.commit()
    assert wc.attempted == 2
    assert wc.inserted == 1
    assert wc.unchanged == 0

    n = await session.scalar(
        text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
        {"sid": sid},
    )
    assert n == 1


# ----- Codex F25 — observation metadata numeric-string-key validation -----


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_write_rejects_numeric_string_metadata_key(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex F25, 2026-04-29: ObservationWriter.write must validate
    each ObservationIn.metadata for numeric-string object keys in
    Phase 0, BEFORE any DB I/O. The forbidden-key contract (codex
    F24) applies uniformly to BOTH ts.series_catalog.metadata AND
    ts.observation.metadata — the Art. 17 deletion runtime walks
    both, and a numeric-string key in either place creates the same
    ``jsonb_set`` ambiguity F24 was meant to eliminate.

    Asserts both the raise AND zero DB I/O (spy on
    AsyncSession.execute) so an implementer can't move the validator
    after Phase 1 (advisory lock acquisition) or Phase 2 (INSERT)
    without the test catching it.
    """
    from aslan_core.errors import MetadataSchemaViolation

    rid = await _new_run(session)
    sid = await _seed_series(session, rid, code="wb.f25.toplevel")
    w = ObservationWriter(session, ingestion_run_id=rid)
    await session.commit()

    execute_calls: list[Any] = []
    original_execute = session.execute

    async def spy_execute(*args: Any, **kwargs: Any) -> Any:
        execute_calls.append(args)
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(session, "execute", spy_execute)

    obs = [
        ObservationIn(
            ts=_ts(1),
            as_of=_ts(2),
            value=1.0,
            metadata={"0": "x"},  # top-level numeric-string key
        ),
    ]
    with pytest.raises(MetadataSchemaViolation, match=r"[Nn]umeric"):
        await w.write(sid, obs)

    # Zero DB I/O — Phase 0 returned BEFORE advisory lock or INSERT.
    assert len(execute_calls) == 0, (
        f"Expected 0 execute calls before Phase 0 raise, got {len(execute_calls)}: {execute_calls}"
    )
    n = await session.scalar(
        text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
        {"sid": sid},
    )
    assert n == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_write_rejects_nested_numeric_string_key(
    session: AsyncSession,
) -> None:
    """Codex F25, 2026-04-29: the validator is recursive — a
    numeric-string key buried under a non-numeric outer key (e.g.
    metadata={"fields": {"0": "y"}}) is still rejected. Same regex
    as the upsert_series guard (``^\\d+$``)."""
    from aslan_core.errors import MetadataSchemaViolation

    rid = await _new_run(session)
    sid = await _seed_series(session, rid, code="wb.f25.nested")
    w = ObservationWriter(session, ingestion_run_id=rid)

    obs = [
        ObservationIn(
            ts=_ts(1),
            as_of=_ts(2),
            value=1.0,
            metadata={"fields": {"0": "y"}},  # nested numeric-string key
        ),
    ]
    with pytest.raises(MetadataSchemaViolation, match=r"[Nn]umeric"):
        await w.write(sid, obs)

    n = await session.scalar(
        text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
        {"sid": sid},
    )
    assert n == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_observation_write_accepts_clean_metadata(
    session: AsyncSession,
) -> None:
    """Codex F25, 2026-04-29: sanity — a clean ObservationIn.metadata
    (non-numeric keys, arrays of values, nested non-numeric keys)
    passes the validator and the row lands."""
    rid = await _new_run(session)
    sid = await _seed_series(session, rid, code="wb.f25.clean")
    w = ObservationWriter(session, ingestion_run_id=rid)

    obs = [
        ObservationIn(
            ts=_ts(1),
            as_of=_ts(2),
            value=1.0,
            metadata={
                "fields": {"item_0": "x", "_1": "y", "row_42": "z"},
                "tags": ["a", "b", "c"],  # arrays OK
            },
        ),
    ]
    wc = await w.write(sid, obs)
    await session.commit()
    assert wc.inserted == 1
