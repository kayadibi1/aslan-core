"""Integration tests — value/value_text + tz-aware contract for
``ObservationWriter.write``.

Plan reference: v0.4.0 implementation plan Task 17. The contract
exists at three layers (Pydantic ObservationIn validators, Postgres
CHECK constraint on (value IS NULL) <> (value_text IS NULL), and
``payload_hash``-time finiteness validation on metadata). This file
verifies each layer fires its rejection at the right boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
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
async def test_value_text_round_trips(session: AsyncSession) -> None:
    """value_text-only observations land with value=NULL, value_text=...
    The Postgres CHECK ``(value IS NULL) <> (value_text IS NULL)`` is
    satisfied by the XOR of the two fields."""
    rid = await _seed(session, "val")
    set_actor(Actor(actor_id="user:val", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    sr = await w.upsert_series(
        series_code="val.text",
        source_id="kap",
        metric="rating",
        frequency="irregular",
        unit="grade",
    )
    await session.commit()

    obs = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 2, tzinfo=UTC),
        value_text="AAA",
    )
    await w.write(sr.series_id, [obs])
    await session.commit()
    row = (
        await session.execute(
            text("SELECT value, value_text FROM ts.observation WHERE series_id = :sid"),
            {"sid": sr.series_id},
        )
    ).one()
    assert row.value is None
    assert row.value_text == "AAA"


def test_observation_in_naive_datetime_rejected_at_construction() -> None:
    """tz-aware enforcement happens at ObservationIn construction — naive
    datetimes never reach the writer."""
    with pytest.raises(ValidationError):
        ObservationIn(
            ts=datetime(2026, 1, 1),  # naive  # noqa: DTZ001
            as_of=datetime(2026, 1, 2, tzinfo=UTC),
            value=1.0,
        )


def test_observation_in_metadata_with_inf_rejected_on_hash() -> None:
    """metadata containing float('inf') / float('nan') survives Pydantic
    construction (the Pydantic model only enforces ts/as_of tz-awareness
    + value/value_text exactly-one), but ``payload_hash()`` raises
    ObservationValidationError before hashing because JSON cannot
    represent non-finite floats (codex F8)."""
    o = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 2, tzinfo=UTC),
        value=1.0,
        metadata={"flag": float("inf")},
    )
    with pytest.raises(ObservationValidationError):
        o.payload_hash()


def test_observation_in_both_value_and_value_text_rejected() -> None:
    """exactly-one-of contract: setting both value and value_text raises
    on construction."""
    with pytest.raises(ValidationError):
        ObservationIn(
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            as_of=datetime(2026, 1, 2, tzinfo=UTC),
            value=1.0,
            value_text="text",
        )


def test_observation_in_neither_value_nor_value_text_rejected() -> None:
    """exactly-one-of contract: neither set also raises."""
    with pytest.raises(ValidationError):
        ObservationIn(
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            as_of=datetime(2026, 1, 2, tzinfo=UTC),
        )
