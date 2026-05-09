"""Integration tests for `aslan_core.dq.alert_dispatch`.

Exercises:

  * ``enqueue`` writes a pending row + the unique-index dedup makes
    a re-enqueue inside the same minute a no-op.
  * ``evaluate_and_enqueue`` finds a freshly-breached
    ``audit.recency_observation`` and lands one
    ``audit.alert_dispatch`` row per sink in
    ``recency_sla_breach.sinks``.
  * ``dispatch_pending`` drains pending rows; status transitions
    correctly for delivered / suppressed / failed sinks (using
    fake in-memory sinks so no httpx/smtp traffic).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.config import Settings
from aslan_core.dq import alert_dispatch
from aslan_core.dq.sinks import Sink, SinkNotConfigured

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


# ── Fake sinks ─────────────────────────────────────────────────────


class _RecordingSink:
    """Captures every call to deliver(); always returns True."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], str, str]] = []

    async def deliver(
        self,
        payload: dict[str, Any],
        severity: str,
        rule_name: str,
    ) -> bool:
        self.calls.append((payload, severity, rule_name))
        return True


class _SuppressedSink:
    """Always raises SinkNotConfigured."""

    async def deliver(
        self,
        payload: dict[str, Any],
        severity: str,
        rule_name: str,
    ) -> bool:
        raise SinkNotConfigured("test-suppression")


class _FailingSink:
    """Always raises a generic exception."""

    async def deliver(
        self,
        payload: dict[str, Any],
        severity: str,
        rule_name: str,
    ) -> bool:
        raise RuntimeError("test-failure")


# ── Fixtures ───────────────────────────────────────────────────────


@pytest_asyncio.fixture(loop_scope="session")
async def _wipe_alert_dispatch(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Clean slate for every test in this file."""
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.alert_dispatch"))
        await s.execute(text("DELETE FROM audit.event WHERE event_type = 'alert_rule_skipped'"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.alert_dispatch"))
        await s.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def _patch_alert_dispatch_engine(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Redirect ``alert_dispatch.dispatch_pending``'s engine creation
    onto the testcontainer session_factory, the same trick the M2 CLI
    tests use for cli/dq.py."""

    class _NoOpEngine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr("aslan_core.dq.alert_dispatch.create_engine", lambda: _NoOpEngine())
    monkeypatch.setattr(
        "aslan_core.dq.alert_dispatch.create_session_factory",
        lambda _e: session_factory,
    )
    yield


def _empty_settings() -> Settings:
    """Settings with all sink config nulled out."""
    return Settings(
        ASLAN_PG_DSN=SecretStr("postgresql://x:y@localhost/test"),
        ASLAN_REDIS_URL="redis://localhost:6379/0",
        ASLAN_S3_ENDPOINT="http://localhost:9000",
        ASLAN_S3_REGION="us-east-1",
        ASLAN_S3_ACCESS_KEY=SecretStr("x"),
        ASLAN_S3_SECRET_KEY=SecretStr("y"),
    )


# ── enqueue ────────────────────────────────────────────────────────


async def test_enqueue_writes_pending_row(
    _wipe_alert_dispatch: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fired_at = datetime.now(UTC)
    async with session_factory() as s:
        dispatch_id = await alert_dispatch.enqueue(
            session=s,
            rule_name="recency_sla_breach",
            fired_at=fired_at,
            sink="glitchtip",
            payload={"source": "kap", "lag_seconds": 1850},
            content_hash="abcdef0123456789",
        )
        await s.commit()
    assert isinstance(dispatch_id, uuid.UUID)
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT rule_name, sink, status, payload, content_hash "
                    "FROM audit.alert_dispatch WHERE dispatch_id = :did"
                ),
                {"did": dispatch_id},
            )
        ).one()
    assert row.rule_name == "recency_sla_breach"
    assert row.sink == "glitchtip"
    assert row.status == "pending"
    assert row.content_hash == "abcdef0123456789"


async def test_enqueue_dedup_within_minute(
    _wipe_alert_dispatch: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same (rule, sink, hash) inside the same minute: second enqueue
    returns the existing dispatch_id; row count remains 1."""
    fired_at = datetime.now(UTC)
    async with session_factory() as s:
        first = await alert_dispatch.enqueue(
            session=s,
            rule_name="recency_sla_breach",
            fired_at=fired_at,
            sink="glitchtip",
            payload={"observation_id": 42},
            content_hash="dedup_hash_abc",
        )
        second = await alert_dispatch.enqueue(
            session=s,
            rule_name="recency_sla_breach",
            # 5 seconds later, still same minute
            fired_at=fired_at + timedelta(seconds=5),
            sink="glitchtip",
            payload={"observation_id": 42},
            content_hash="dedup_hash_abc",
        )
        await s.commit()
    assert first == second  # Same row.
    async with session_factory() as s:
        count = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.alert_dispatch "
                    "WHERE rule_name = 'recency_sla_breach' "
                    "  AND content_hash = 'dedup_hash_abc'"
                )
            )
        ).scalar_one()
    assert count == 1


async def test_enqueue_dedup_resets_next_minute(
    _wipe_alert_dispatch: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An enqueue 60+ seconds later lands a new row (different minute
    bucket → unique-index allows it)."""
    base = datetime(2026, 5, 9, 12, 0, 30, tzinfo=UTC)
    async with session_factory() as s:
        first = await alert_dispatch.enqueue(
            session=s,
            rule_name="recency_sla_breach",
            fired_at=base,
            sink="glitchtip",
            payload={"observation_id": 99},
            content_hash="reset_hash",
        )
        second = await alert_dispatch.enqueue(
            session=s,
            rule_name="recency_sla_breach",
            fired_at=base + timedelta(seconds=60),  # rolls into next minute
            sink="glitchtip",
            payload={"observation_id": 99},
            content_hash="reset_hash",
        )
        await s.commit()
    assert first != second
    async with session_factory() as s:
        count = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.alert_dispatch "
                    "WHERE content_hash = 'reset_hash'"
                )
            )
        ).scalar_one()
    assert count == 2


# ── evaluate_and_enqueue ──────────────────────────────────────────


async def test_evaluate_and_enqueue_picks_up_recency_breach(
    _wipe_alert_dispatch: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fresh recency_observation with sla_breached=true must trigger
    one alert_dispatch row per sink in the recency_sla_breach rule."""
    # Insert a recency_observation row that breaches the SLA (the
    # ``sla_breached`` column is generated; lag = 600s, target = 300s).
    upstream = datetime.now(UTC)
    db_latest = upstream - timedelta(seconds=600)
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.recency_observation"))
        await s.execute(
            text(
                "INSERT INTO audit.recency_observation("
                "  source, observed_at, upstream_latest_at, db_latest_at, "
                "  sla_target_seconds"
                ") VALUES ("
                "  'kap', :observed_at, :upstream_latest_at, :db_latest_at, 300"
                ")"
            ),
            {
                "observed_at": upstream,
                "upstream_latest_at": upstream,
                "db_latest_at": db_latest,
            },
        )
        await s.commit()

    async with session_factory() as s:
        enqueued = await alert_dispatch.evaluate_and_enqueue(session=s)
        await s.commit()
    # recency_sla_breach (glitchtip + slack) and
    # recency_sla_breach_2x (glitchtip + slack) both fire because lag
    # (600s) > 2 * sla_target (300s); 4 rows enqueued.
    assert enqueued >= 2  # At minimum recency_sla_breach fires for both sinks.
    async with session_factory() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT rule_name, sink FROM audit.alert_dispatch "
                    "WHERE rule_name LIKE 'recency_%' "
                    "ORDER BY rule_name, sink"
                )
            )
        ).all()
    pairs = {(r.rule_name, r.sink) for r in rows}
    assert ("recency_sla_breach", "glitchtip") in pairs
    assert ("recency_sla_breach", "slack") in pairs

    # Re-evaluate within the same minute → no new rows.
    async with session_factory() as s:
        re_enqueued = await alert_dispatch.evaluate_and_enqueue(session=s)
        await s.commit()
    assert re_enqueued == 0


async def test_evaluate_and_enqueue_skips_missing_table_for_bloomberg(
    _wipe_alert_dispatch: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`bloomberg_loses_field` and `regression_flag_critical_entity`
    target tables that don't exist on this branch (M4/M5). The
    dispatcher must skip them silently after emitting an
    `alert_rule_skipped` event — no INSERT to alert_dispatch."""
    async with session_factory() as s:
        await alert_dispatch.evaluate_and_enqueue(session=s)
        await s.commit()
    async with session_factory() as s:
        skipped_for_bloomberg = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.event "
                    "WHERE event_type = 'alert_rule_skipped' "
                    "  AND payload->>'rule_name' = 'bloomberg_loses_field'"
                )
            )
        ).scalar_one()
        bloomberg_dispatches = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.alert_dispatch "
                    "WHERE rule_name = 'bloomberg_loses_field'"
                )
            )
        ).scalar_one()
    assert skipped_for_bloomberg >= 1
    assert bloomberg_dispatches == 0


# ── dispatch_pending ──────────────────────────────────────────────


async def test_dispatch_pending_delivered_status(
    _wipe_alert_dispatch: None,
    _patch_alert_dispatch_engine: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pending row whose sink returns True transitions to 'delivered'."""
    async with session_factory() as s:
        await alert_dispatch.enqueue(
            session=s,
            rule_name="recency_sla_breach",
            fired_at=datetime.now(UTC),
            sink="glitchtip",
            payload={"source": "kap"},
            content_hash="delivered_hash",
        )
        await s.commit()
    sinks: dict[str, Sink] = {
        "glitchtip": _RecordingSink(),
        "email": _RecordingSink(),
        "slack": _RecordingSink(),
    }
    delivered = await alert_dispatch.dispatch_pending(settings=_empty_settings(), sinks=sinks)
    assert delivered == 1
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, delivered_at FROM audit.alert_dispatch "
                    "WHERE content_hash = 'delivered_hash'"
                )
            )
        ).one()
    assert row.status == "delivered"
    assert row.delivered_at is not None


async def test_dispatch_pending_suppressed_when_sink_not_configured(
    _wipe_alert_dispatch: None,
    _patch_alert_dispatch_engine: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A SinkNotConfigured raise → status='suppressed'."""
    async with session_factory() as s:
        await alert_dispatch.enqueue(
            session=s,
            rule_name="recency_sla_breach",
            fired_at=datetime.now(UTC),
            sink="email",
            payload={"source": "kap"},
            content_hash="suppressed_hash",
        )
        await s.commit()
    sinks: dict[str, Sink] = {
        "glitchtip": _RecordingSink(),
        "email": _SuppressedSink(),
        "slack": _RecordingSink(),
    }
    delivered = await alert_dispatch.dispatch_pending(settings=_empty_settings(), sinks=sinks)
    assert delivered == 0
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, payload FROM audit.alert_dispatch "
                    "WHERE content_hash = 'suppressed_hash'"
                )
            )
        ).one()
    assert row.status == "suppressed"
    assert "_dispatch_suppressed_reason" in row.payload


async def test_dispatch_pending_failed_status(
    _wipe_alert_dispatch: None,
    _patch_alert_dispatch_engine: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Generic sink exception → status='failed' with error in payload."""
    async with session_factory() as s:
        await alert_dispatch.enqueue(
            session=s,
            rule_name="recency_sla_breach",
            fired_at=datetime.now(UTC),
            sink="slack",
            payload={"source": "kap"},
            content_hash="failed_hash",
        )
        await s.commit()
    sinks: dict[str, Sink] = {
        "glitchtip": _RecordingSink(),
        "email": _RecordingSink(),
        "slack": _FailingSink(),
    }
    delivered = await alert_dispatch.dispatch_pending(settings=_empty_settings(), sinks=sinks)
    assert delivered == 0
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, payload FROM audit.alert_dispatch "
                    "WHERE content_hash = 'failed_hash'"
                )
            )
        ).one()
    assert row.status == "failed"
    assert row.payload["_dispatch_error"] == "test-failure"


async def test_dispatch_pending_unknown_sink_marked_failed(
    _wipe_alert_dispatch: None,
    _patch_alert_dispatch_engine: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pending row whose sink isn't in the sinks dict transitions
    to 'failed' with a clear diagnostic — not delivered, not suppressed."""
    async with session_factory() as s:
        await alert_dispatch.enqueue(
            session=s,
            rule_name="recency_sla_breach",
            fired_at=datetime.now(UTC),
            sink="webhook_typo",
            payload={"source": "kap"},
            content_hash="unknown_hash",
        )
        await s.commit()
    sinks: dict[str, Sink] = {"glitchtip": _RecordingSink()}
    await alert_dispatch.dispatch_pending(settings=_empty_settings(), sinks=sinks)
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, payload FROM audit.alert_dispatch "
                    "WHERE content_hash = 'unknown_hash'"
                )
            )
        ).one()
    assert row.status == "failed"
    assert "unknown sink" in str(row.payload.get("_dispatch_error", ""))
