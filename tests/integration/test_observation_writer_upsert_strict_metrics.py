"""Integration tests — strict-mode + Prometheus + concurrency
idempotency regression for ``ObservationWriter.upsert_series``.

Plan reference: v0.4.0 Task 13. Verifies:

* :class:`AuditMissingActor` is raised BEFORE any DB I/O when
  ``audit_strict=True`` and no actor is set in the ContextVar.
* The ``aslan_series_upserts_total{source_id, frequency}`` counter
  increments on every upsert call.
* Codex F1-style concurrency idempotency: two actors call
  ``upsert_series`` with the same ``series_code``; the row's
  ``actor_id`` stays with the FIRST actor; the audit log shows one
  ``series.upsert`` and one ``series.idempotent_hit`` event.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import Actor, set_actor
from aslan_core.errors import AuditMissingActor
from aslan_core.observability.metrics import series_upserts
from aslan_core.timeseries import ObservationWriter

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_default_actor() -> Iterator[None]:
    """Opt out of the conftest autouse default actor so the strict-mode
    test runs against an empty ContextVar."""
    set_actor(None)
    yield
    set_actor(None)


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
            "VALUES ('kap', 'strict') RETURNING ingestion_run_id"
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
async def test_strict_mode_upsert_raises_without_actor(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASLAN_AUDIT_STRICT", "true")
    rid = await _new_run(session)
    w = ObservationWriter(session, ingestion_run_id=rid)
    with pytest.raises(AuditMissingActor):
        await w.upsert_series(
            series_code="strict.x",
            source_id="kap",
            metric="m",
            frequency="1d",
            unit="TRY",
        )

    # No row was inserted, no audit event written.
    n = await session.scalar(
        text("SELECT COUNT(*) FROM ts.series_catalog WHERE series_code = 'strict.x'")
    )
    assert n == 0
    n = await session.scalar(
        text(
            "SELECT COUNT(*) FROM audit.events "
            "WHERE target_schema='ts' AND target_table='series_catalog'"
        )
    )
    assert n == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_upsert_series_increments_prometheus_counter(
    session: AsyncSession,
) -> None:
    set_actor(Actor(actor_id="user:metric", actor_kind="user"))
    rid = await _new_run(session)
    w = ObservationWriter(session, ingestion_run_id=rid)

    before = series_upserts.labels(source_id="kap", frequency="1d")._value.get()
    await w.upsert_series(
        series_code="metric.test",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()
    after = series_upserts.labels(source_id="kap", frequency="1d")._value.get()
    assert after == before + 1


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_upsert_idempotency_preserves_first_actor_attribution(
    session: AsyncSession,
) -> None:
    """Codex F1 regression: two actors race on the same series_code;
    only one fresh INSERT lands; the row's audit cols stay with the
    FIRST actor; the audit log shows one ``series.upsert`` (first
    actor) and one ``series.idempotent_hit`` (second actor).

    This is the v0.4 mirror of the v0.3 ``add_identifier`` /
    ``put_filing`` idempotent-hit attribution test."""
    rid = await _new_run(session)

    # First actor lands the fresh INSERT.
    set_actor(Actor(actor_id="user:first", actor_kind="user"))
    w1 = ObservationWriter(session, ingestion_run_id=rid)
    r1 = await w1.upsert_series(
        series_code="conc.idem",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()
    assert r1.created is True

    # Second actor's call is idempotent — same fields → returns same id.
    set_actor(Actor(actor_id="user:second", actor_kind="user"))
    w2 = ObservationWriter(session, ingestion_run_id=rid)
    r2 = await w2.upsert_series(
        series_code="conc.idem",
        source_id="kap",
        metric="m",
        frequency="1d",
        unit="TRY",
    )
    await session.commit()
    assert r2.created is False
    assert r2.series_id == r1.series_id

    # The row's actor_id stays with the FIRST actor.
    row = (
        await session.execute(
            text("SELECT actor_id FROM ts.series_catalog WHERE series_id = :sid"),
            {"sid": r1.series_id},
        )
    ).one()
    assert row.actor_id == "user:first"

    # The audit log carries one upsert + one idempotent_hit, attributed
    # to first + second respectively.
    rows = (
        await session.execute(
            text(
                "SELECT operation, actor_id FROM audit.events "
                "WHERE target_schema='ts' AND target_table='series_catalog' "
                "ORDER BY occurred_at"
            )
        )
    ).all()
    assert [(r.operation, r.actor_id) for r in rows] == [
        ("series.upsert", "user:first"),
        ("series.idempotent_hit", "user:second"),
    ]
