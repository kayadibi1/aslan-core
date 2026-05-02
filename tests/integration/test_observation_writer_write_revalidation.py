"""Integration tests — Phase 0 ``write()`` re-validation contract
(codex Batch 3 F3, 2026-04-29).

``ObservationIn`` Pydantic validators enforce tz-aware ts/as_of and
value/value_text exactly-one at construction, but
``model_construct`` (and equivalent non-validating bypasses) skip
those validators. ``write()`` is the authoritative validation
boundary — re-check every row regardless of how the ObservationIn was
built so a future caller can't sneak invalid rows into the DB by
bypassing Pydantic.

Each test uses the F14 execute-spy pattern to verify ZERO DB I/O
runs before the raise. If an implementer reorders ``write()`` so the
advisory lock acquisition (Phase 1) or a source_id lookup runs
*before* the re-validation, the spy assertion catches it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
from aslan_core.errors import ObservationValidationError
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
            "VALUES ('kap', 'reval') RETURNING ingestion_run_id"
        )
    )
    await session.commit()
    return int(rid)


async def _seed_series(session: AsyncSession, rid: int, code: str) -> int:
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


@pytest.mark.asyncio(loop_scope="session")
async def test_write_rejects_naive_ts_constructed_via_model_construct(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Batch 3 F3: ``ObservationIn.model_construct`` bypasses the
    Pydantic ``_tz_aware`` validator. ``write()`` MUST re-check
    tz-awareness and raise ``ObservationValidationError`` BEFORE any
    DB I/O.
    """
    rid = await _new_run(session)
    sid = await _seed_series(session, rid, code="reval.naive_ts")
    w = ObservationWriter(session, ingestion_run_id=rid)
    await session.commit()

    execute_calls: list[Any] = []
    original_execute = session.execute

    async def spy_execute(*args: Any, **kwargs: Any) -> Any:
        execute_calls.append(args)
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(session, "execute", spy_execute)

    # model_construct bypasses Pydantic validators; the resulting
    # ObservationIn carries a naive ts.
    bad = ObservationIn.model_construct(
        ts=datetime(2026, 1, 1),  # naive  # noqa: DTZ001
        as_of=datetime(2026, 1, 2, tzinfo=UTC),
        value=1.0,
        value_text=None,
        quality_flag=0,
        metadata={},
    )
    with pytest.raises(ObservationValidationError, match="naive"):
        await w.write(sid, [bad])

    assert len(execute_calls) == 0, (
        f"expected 0 execute calls before Phase 0a raise, got {len(execute_calls)}"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_write_rejects_naive_as_of_constructed_via_model_construct(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Batch 3 F3: same contract for ``as_of`` — both ts AND
    as_of must be tz-aware at the write boundary.
    """
    rid = await _new_run(session)
    sid = await _seed_series(session, rid, code="reval.naive_as_of")
    w = ObservationWriter(session, ingestion_run_id=rid)
    await session.commit()

    execute_calls: list[Any] = []
    original_execute = session.execute

    async def spy_execute(*args: Any, **kwargs: Any) -> Any:
        execute_calls.append(args)
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(session, "execute", spy_execute)

    bad = ObservationIn.model_construct(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 2),  # naive  # noqa: DTZ001
        value=1.0,
        value_text=None,
        quality_flag=0,
        metadata={},
    )
    with pytest.raises(ObservationValidationError, match="naive"):
        await w.write(sid, [bad])

    assert len(execute_calls) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_write_rejects_both_value_and_value_text_via_model_construct(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Batch 3 F3: ``model_construct`` bypasses the
    ``_exactly_one_value`` model validator. ``write()`` MUST re-check
    and raise BEFORE any DB I/O when both fields are set.
    """
    rid = await _new_run(session)
    sid = await _seed_series(session, rid, code="reval.both")
    w = ObservationWriter(session, ingestion_run_id=rid)
    await session.commit()

    execute_calls: list[Any] = []
    original_execute = session.execute

    async def spy_execute(*args: Any, **kwargs: Any) -> Any:
        execute_calls.append(args)
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(session, "execute", spy_execute)

    bad = ObservationIn.model_construct(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 2, tzinfo=UTC),
        value=1.0,
        value_text="text",  # both set — invalid
        quality_flag=0,
        metadata={},
    )
    with pytest.raises(ObservationValidationError, match="exactly one"):
        await w.write(sid, [bad])

    assert len(execute_calls) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_write_rejects_neither_value_nor_value_text_via_model_construct(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Batch 3 F3: neither field set is also invalid; the
    Pydantic ``_exactly_one_value`` validator catches it at
    construction, ``model_construct`` bypasses it. ``write()`` MUST
    re-check and raise BEFORE any DB I/O.
    """
    rid = await _new_run(session)
    sid = await _seed_series(session, rid, code="reval.neither")
    w = ObservationWriter(session, ingestion_run_id=rid)
    await session.commit()

    execute_calls: list[Any] = []
    original_execute = session.execute

    async def spy_execute(*args: Any, **kwargs: Any) -> Any:
        execute_calls.append(args)
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(session, "execute", spy_execute)

    bad = ObservationIn.model_construct(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 2, tzinfo=UTC),
        value=None,
        value_text=None,  # neither set — invalid
        quality_flag=0,
        metadata={},
    )
    with pytest.raises(ObservationValidationError, match="exactly one"):
        await w.write(sid, [bad])

    assert len(execute_calls) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_write_revalidation_runs_for_every_row_not_just_the_first(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Batch 3 F3: re-validation walks the WHOLE batch — a
    single bad row anywhere in the iterable raises before Phase 1.
    Index in the message tells the caller which row was rejected.
    """
    rid = await _new_run(session)
    sid = await _seed_series(session, rid, code="reval.middle")
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
            ts=datetime(2026, 9, 1, tzinfo=UTC),
            as_of=datetime(2026, 9, 2, tzinfo=UTC),
            value=1.0,
        ),
        # Bad row in position 1 — Pydantic-bypassed.
        ObservationIn.model_construct(
            ts=datetime(2026, 9, 3),  # naive  # noqa: DTZ001
            as_of=datetime(2026, 9, 4, tzinfo=UTC),
            value=2.0,
            value_text=None,
            quality_flag=0,
            metadata={},
        ),
        ObservationIn(
            ts=datetime(2026, 9, 5, tzinfo=UTC),
            as_of=datetime(2026, 9, 6, tzinfo=UTC),
            value=3.0,
        ),
    ]
    with pytest.raises(ObservationValidationError, match=r"observations\[1\]"):
        await w.write(sid, obs)

    assert len(execute_calls) == 0
