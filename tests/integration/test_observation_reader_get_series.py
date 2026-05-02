"""Integration tests — ``ObservationReader.get_series``.

Plan reference: v0.4.0 Task 20. ``get_series(series_code)`` returns a
:class:`Series` Pydantic model with subjects round-tripped from
``ts.series_subject``. ``None`` when the row does not exist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
from aslan_core.schemas.timeseries import SubjectRef
from aslan_core.timeseries import ObservationReader, ObservationWriter

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


async def _new_run(session: AsyncSession, job: str = "r-gs") -> int:
    await _seed_sources(session)
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
async def test_get_series_returns_none_when_missing(session: AsyncSession) -> None:
    r = ObservationReader(session)
    assert await r.get_series("not.exist.code") is None


@pytest.mark.asyncio(loop_scope="session")
async def test_get_series_round_trips_full_catalog_row(session: AsyncSession) -> None:
    """A full catalog row is hydrated into a :class:`Series` — every
    persisted column reflected, ``subjects`` empty when none linked.
    """
    rid = await _new_run(session, job="r-gs-full")
    set_actor(Actor(actor_id="user:r", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    await w.upsert_series(
        series_code="get.full.1",
        source_id="kap",
        metric="rev",
        frequency="1q",
        unit="TRY",
        restatement_basis="restated",
        accounting_standard="ifrs",
        consolidation="consolidated",
        period_type="quarterly",
        description="quarterly revenue",
        metadata={"src_label": "tcmb-q1", "version": 2},
    )
    await session.commit()

    r = ObservationReader(session)
    s = await r.get_series("get.full.1")
    assert s is not None
    assert s.series_code == "get.full.1"
    assert s.source_id == "kap"
    assert s.metric == "rev"
    assert s.frequency == "1q"
    assert s.unit == "TRY"
    assert s.currency_code is None
    assert s.restatement_basis == "restated"
    assert s.accounting_standard == "ifrs"
    assert s.consolidation == "consolidated"
    assert s.period_type == "quarterly"
    assert s.description == "quarterly revenue"
    assert s.pii_class == "none"
    assert s.metadata == {"src_label": "tcmb-q1", "version": 2}
    assert s.subjects == ()
    assert s.created_at is not None
    assert s.updated_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_get_series_round_trips_single_subject(session: AsyncSession) -> None:
    rid = await _new_run(session, job="r-gs-1subj")
    set_actor(Actor(actor_id="user:r", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    await w.upsert_series(
        series_code="get.1subj",
        source_id="kap",
        metric="comp",
        frequency="1y",
        unit="TRY",
        pii_class="identifying",
        subjects=(SubjectRef(subject_id="kap-person-001", role="executive"),),
    )
    await session.commit()

    r = ObservationReader(session)
    s = await r.get_series("get.1subj")
    assert s is not None
    assert s.subjects == (SubjectRef(subject_id="kap-person-001", role="executive"),)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_series_round_trips_multiple_subjects(session: AsyncSession) -> None:
    """Subjects are returned in (subject_id, role) order so the
    sequence is deterministic regardless of insertion order."""
    rid = await _new_run(session, job="r-gs-multi")
    set_actor(Actor(actor_id="user:r", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    await w.upsert_series(
        series_code="get.multi",
        source_id="kap",
        metric="comp",
        frequency="1y",
        unit="TRY",
        pii_class="identifying",
        # Pass the larger subject_id first; the reader must still
        # return them in subject_id ASC order.
        subjects=(
            SubjectRef(subject_id="zz-person-002", role="board_member"),
            SubjectRef(subject_id="aa-person-001", role="executive"),
        ),
    )
    await session.commit()

    r = ObservationReader(session)
    s = await r.get_series("get.multi")
    assert s is not None
    assert s.subjects == (
        SubjectRef(subject_id="aa-person-001", role="executive"),
        SubjectRef(subject_id="zz-person-002", role="board_member"),
    )
