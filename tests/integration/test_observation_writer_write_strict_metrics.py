"""Integration tests — strict-mode early reject + Prometheus + ``@traced``
contract for ``ObservationWriter.write``.

Plan reference: v0.4.0 implementation plan Task 19.

Strict-mode contract (codex F3, v0.3): ``audit_strict=True`` AND no
actor in the ContextVar → raise :class:`AuditMissingActor` BEFORE
any DB I/O. The procurement-grade default for customer-facing
service profiles depends on this raising before any side effect runs.

Prometheus contract: ``aslan_observation_writes_total{source_id,
kind="bulk"}.inc(len(observations))`` and
``aslan_observation_write_batch_size.observe(len(observations))``.
The histogram + counter are wired AFTER Phase 0 so a structurally
invalid batch does not bump the metric.

This file opts OUT of conftest's autouse default-actor fixture for
the strict-mode test so the no-actor branch actually fires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
from aslan_core.errors import AuditMissingActor
from aslan_core.observability.metrics import (
    observation_write_batch_size,
    observation_writes,
)
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


@pytest.fixture
def _no_default_actor() -> Iterator[None]:
    """Locally clear the conftest autouse default actor so strict mode
    actually sees no actor (without this every test inherits
    ``user:pytest``). Mirrors the pattern in
    ``test_strict_actor_enforcement.py``."""
    set_actor(None)
    yield
    set_actor(None)


@pytest.mark.asyncio(loop_scope="session")
async def test_strict_write_raises_without_actor(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    _no_default_actor: None,
) -> None:
    """v0.4 Task 19 — strict-mode early reject. ``audit_strict=True``
    AND no actor → raise :class:`AuditMissingActor` BEFORE any I/O.

    Procurement-grade contract: no observation row, no audit event,
    no Prometheus increment land when strict mode catches a missing
    actor. The implementation calls ``assert_actor_or_strict_raise()``
    at the top of ``write()`` BEFORE Phase 0.
    """
    monkeypatch.setenv("ASLAN_AUDIT_STRICT", "true")
    rid = await _seed(session, "sw")
    set_actor(Actor(actor_id="user:setup", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    sr = await w.upsert_series(
        series_code="sw.1",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    # Snapshot pre-state to confirm zero side effects on raise.
    pre_obs = await session.scalar(
        text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
        {"sid": sr.series_id},
    )
    pre_events = await session.scalar(
        text(
            "SELECT COUNT(*) FROM audit.events "
            "WHERE target_schema='ts' AND target_table='observation'"
        )
    )

    set_actor(None)
    with pytest.raises(AuditMissingActor):
        await w.write(
            sr.series_id,
            [
                ObservationIn(
                    ts=datetime(2026, 1, 1, tzinfo=UTC),
                    as_of=datetime(2026, 1, 2, tzinfo=UTC),
                    value=1.0,
                ),
            ],
        )

    # Zero side effects: strict mode caught the missing actor BEFORE
    # any DB I/O.
    post_obs = await session.scalar(
        text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
        {"sid": sr.series_id},
    )
    post_events = await session.scalar(
        text(
            "SELECT COUNT(*) FROM audit.events "
            "WHERE target_schema='ts' AND target_table='observation'"
        )
    )
    assert post_obs == pre_obs
    assert post_events == pre_events


@pytest.mark.asyncio(loop_scope="session")
async def test_write_increments_counter_by_attempted_count(
    session: AsyncSession,
) -> None:
    """v0.4 Task 19 — ``aslan_observation_writes_total{source_id, kind=bulk}``
    increments by ``len(observations)``, NOT batch count. The
    source_id label is resolved from ts.series_catalog so cardinality
    is bounded by the enumerated source set.
    """
    pytest.importorskip("prometheus_client")

    rid = await _seed(session, "mtr")
    set_actor(Actor(actor_id="user:m", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    sr = await w.upsert_series(
        series_code="mtr.1",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    impl = observation_writes._ensure_impl()
    assert impl is not None
    before = impl.labels(source_id="kap", kind="bulk")._value.get()
    obs = [
        ObservationIn(
            ts=datetime(2026, 5, d, tzinfo=UTC),
            as_of=datetime(2026, 5, d + 1, tzinfo=UTC),
            value=float(d),
        )
        for d in range(1, 4)
    ]
    await w.write(sr.series_id, obs)
    await session.commit()
    after = impl.labels(source_id="kap", kind="bulk")._value.get()
    # Inc by 3 — the observation count, not the batch count of 1.
    assert after == before + 3


@pytest.mark.asyncio(loop_scope="session")
async def test_write_observes_batch_size_histogram(
    session: AsyncSession,
) -> None:
    """v0.4 Task 19 — ``aslan_observation_write_batch_size.observe(N)``
    fires once per ``write()`` call with the attempted-row count."""
    pytest.importorskip("prometheus_client")

    rid = await _seed(session, "hst")
    set_actor(Actor(actor_id="user:h", actor_kind="user"))
    w = ObservationWriter(session, ingestion_run_id=rid)
    sr = await w.upsert_series(
        series_code="hst.1",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()

    impl = observation_write_batch_size._ensure_impl()
    assert impl is not None
    samples_before = list(impl.collect()[0].samples)

    obs = [
        ObservationIn(
            ts=datetime(2026, 7, d, tzinfo=UTC),
            as_of=datetime(2026, 7, d + 1, tzinfo=UTC),
            value=float(d),
        )
        for d in range(1, 6)
    ]
    await w.write(sr.series_id, obs)
    await session.commit()

    samples_after = list(impl.collect()[0].samples)
    # Sum-bucket increased by len(obs) — the histogram observed exactly
    # one sample with value 5.
    sum_before = next((s.value for s in samples_before if s.name.endswith("_sum")), 0.0)
    sum_after = next((s.value for s in samples_after if s.name.endswith("_sum")), 0.0)
    count_before = next((s.value for s in samples_before if s.name.endswith("_count")), 0.0)
    count_after = next((s.value for s in samples_after if s.name.endswith("_count")), 0.0)
    assert count_after == count_before + 1
    assert sum_after - sum_before == 5
