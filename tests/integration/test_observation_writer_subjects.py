"""Integration tests — subjects round-trip into ``ts.series_subject``.

Plan reference: v0.4.0 Task 12. ``subjects: tuple[SubjectRef, ...]``
persists rows into ``ts.series_subject`` in the same transaction as
the catalog insert/update. Idempotent at the
``(series_id, subject_id, role)`` level via
``ON CONFLICT DO NOTHING``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
from aslan_core.schemas.timeseries import SubjectRef
from aslan_core.timeseries import ObservationWriter

pytestmark = pytest.mark.integration


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
            "VALUES ('kap', 'test') RETURNING ingestion_run_id"
        )
    )
    await session.commit()
    return int(rid)


async def _wipe(session: AsyncSession) -> None:
    for stmt in [
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


@pytest.mark.asyncio(loop_scope="session")
async def test_subjects_persist_into_series_subject(session: AsyncSession) -> None:
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)

    r = await w.upsert_series(
        series_code="exec.cmptn.opaque.subj",
        source_id="kap",
        metric="comp",
        frequency="1y",
        unit="TRY",
        pii_class="identifying",
        subjects=(
            SubjectRef(subject_id="kap-person-001", role="executive"),
            SubjectRef(subject_id="kap-person-002", role="board_member"),
        ),
    )
    await session.commit()

    rows = (
        await session.execute(
            text(
                "SELECT subject_id, role FROM ts.series_subject "
                "WHERE series_id = :sid ORDER BY subject_id"
            ),
            {"sid": r.series_id},
        )
    ).all()
    assert [(r.subject_id, r.role) for r in rows] == [
        ("kap-person-001", "executive"),
        ("kap-person-002", "board_member"),
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_subjects_idempotent_on_repeated_upsert(session: AsyncSession) -> None:
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)

    subj = (SubjectRef(subject_id="kap-person-x", role="executive"),)
    r1 = await w.upsert_series(
        series_code="subj.idem",
        source_id="kap",
        metric="m",
        frequency="1y",
        unit="TRY",
        pii_class="identifying",
        subjects=subj,
    )
    await session.commit()
    r2 = await w.upsert_series(
        series_code="subj.idem",
        source_id="kap",
        metric="m",
        frequency="1y",
        unit="TRY",
        pii_class="identifying",
        subjects=subj,
    )
    await session.commit()
    assert r1.series_id == r2.series_id

    n = await session.scalar(
        text("SELECT COUNT(*) FROM ts.series_subject WHERE series_id = :sid"),
        {"sid": r1.series_id},
    )
    assert n == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_added_subject_on_subsequent_call_appends(session: AsyncSession) -> None:
    rid = await _new_run(session)
    set_actor(Actor(actor_id="user:t", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    r = await w.upsert_series(
        series_code="subj.append",
        source_id="kap",
        metric="m",
        frequency="1y",
        unit="TRY",
        pii_class="identifying",
        subjects=(SubjectRef(subject_id="p1", role="executive"),),
    )
    await session.commit()

    # Add another subject — first row stays, second is appended.
    await w.upsert_series(
        series_code="subj.append",
        source_id="kap",
        metric="m",
        frequency="1y",
        unit="TRY",
        pii_class="identifying",
        subjects=(
            SubjectRef(subject_id="p1", role="executive"),
            SubjectRef(subject_id="p2", role="board_member"),
        ),
    )
    await session.commit()

    rows = (
        await session.execute(
            text(
                "SELECT subject_id, role FROM ts.series_subject "
                "WHERE series_id = :sid ORDER BY subject_id"
            ),
            {"sid": r.series_id},
        )
    ).all()
    assert [(r.subject_id, r.role) for r in rows] == [
        ("p1", "executive"),
        ("p2", "board_member"),
    ]
