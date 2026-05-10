"""Integration tests for `aslan-core audit alert-dispatch` and
`aslan-core audit test-alert` CLI commands.

Both commands are exercised through their inner async helpers
(``_run_alert_dispatch``, ``_run_test_alert``) with the testcontainer
``session_factory`` patched in for the engine, mirroring the recency-
sweep / coverage-snapshot / spot-check-draw test pattern.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.cli.dq import _run_alert_dispatch, _run_test_alert
from aslan_core.config import Settings
from aslan_core.dq.sinks import Sink, SinkNotConfigured

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


# ── Fake sinks (shared) ────────────────────────────────────────────


class _RecordingSink:
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
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], str, str]] = []

    async def deliver(
        self,
        payload: dict[str, Any],
        severity: str,
        rule_name: str,
    ) -> bool:
        self.calls.append((payload, severity, rule_name))
        raise SinkNotConfigured("not wired")


def _empty_settings() -> Settings:
    return Settings(
        ASLAN_PG_DSN=SecretStr("postgresql://x:y@localhost/test"),
        ASLAN_REDIS_URL="redis://localhost:6379/0",
        ASLAN_S3_ENDPOINT="http://localhost:9000",
        ASLAN_S3_REGION="us-east-1",
        ASLAN_S3_ACCESS_KEY=SecretStr("x"),
        ASLAN_S3_SECRET_KEY=SecretStr("y"),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def _patch_cli_engine(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    class _NoOpEngine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr("aslan_core.cli.dq.create_engine", lambda: _NoOpEngine())
    monkeypatch.setattr(
        "aslan_core.cli.dq.create_session_factory",
        lambda _e: session_factory,
    )
    monkeypatch.setattr("aslan_core.dq.alert_dispatch.create_engine", lambda: _NoOpEngine())
    monkeypatch.setattr(
        "aslan_core.dq.alert_dispatch.create_session_factory",
        lambda _e: session_factory,
    )
    yield


@pytest_asyncio.fixture(loop_scope="session")
async def _wipe(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.alert_dispatch"))
        await s.execute(text("DELETE FROM audit.recency_observation"))
        await s.execute(text("DELETE FROM audit.event WHERE event_type = 'test_alert_emitted'"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.alert_dispatch"))
        await s.execute(text("DELETE FROM audit.recency_observation"))
        await s.execute(text("DELETE FROM audit.event WHERE event_type = 'test_alert_emitted'"))
        await s.commit()


# ── alert-dispatch ────────────────────────────────────────────────


async def test_alert_dispatch_full_loop_enqueues_and_drains(
    _wipe: None,
    _patch_cli_engine: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end: seed a recency_observation breach → run the full
    loop → both glitchtip and slack sinks see the alert; row goes
    delivered."""
    upstream = datetime.now(UTC)
    db_latest = upstream - timedelta(seconds=400)
    async with session_factory() as s:
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

    sinks: dict[str, Sink] = {
        "glitchtip": _RecordingSink(),
        "email": _RecordingSink(),
        "slack": _RecordingSink(),
    }
    summary = await _run_alert_dispatch(
        evaluate_only=False,
        dispatch_only=False,
        settings=_empty_settings(),
        sinks=sinks,
    )
    assert summary["enqueued"] >= 1
    assert summary["delivered"] >= 1
    glitchtip = sinks["glitchtip"]
    slack = sinks["slack"]
    assert isinstance(glitchtip, _RecordingSink)
    assert isinstance(slack, _RecordingSink)
    rule_names_seen = {c[2] for c in glitchtip.calls + slack.calls}
    assert "recency_sla_breach" in rule_names_seen


async def test_alert_dispatch_evaluate_only_does_not_call_sinks(
    _wipe: None,
    _patch_cli_engine: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    upstream = datetime.now(UTC)
    db_latest = upstream - timedelta(seconds=400)
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO audit.recency_observation("
                "  source, observed_at, upstream_latest_at, db_latest_at, "
                "  sla_target_seconds"
                ") VALUES ("
                "  'bist', :observed_at, :upstream_latest_at, :db_latest_at, 300"
                ")"
            ),
            {
                "observed_at": upstream,
                "upstream_latest_at": upstream,
                "db_latest_at": db_latest,
            },
        )
        await s.commit()
    glitchtip = _RecordingSink()
    sinks: dict[str, Sink] = {
        "glitchtip": glitchtip,
        "email": _RecordingSink(),
        "slack": _RecordingSink(),
    }
    summary = await _run_alert_dispatch(
        evaluate_only=True,
        dispatch_only=False,
        settings=_empty_settings(),
        sinks=sinks,
    )
    assert summary["delivered"] == 0
    assert glitchtip.calls == []
    async with session_factory() as s:
        pending = (
            await s.execute(
                text("SELECT count(*)::int AS n FROM audit.alert_dispatch WHERE status = 'pending'")
            )
        ).scalar_one()
    assert pending >= 1


async def test_alert_dispatch_dispatch_only_skips_evaluator(
    _wipe: None,
    _patch_cli_engine: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pre-seed a pending row directly; dispatch-only delivers it
    without running the evaluator."""
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO audit.alert_dispatch("
                "  rule_name, fired_at, sink, status, payload, content_hash"
                ") VALUES ("
                "  'recency_sla_breach', now(), 'glitchtip', 'pending', "
                "  CAST('{\"src\":\"kap\"}' AS JSONB), 'cli_dispatch_only'"
                ")"
            )
        )
        await s.commit()
    glitchtip = _RecordingSink()
    sinks: dict[str, Sink] = {
        "glitchtip": glitchtip,
        "email": _RecordingSink(),
        "slack": _RecordingSink(),
    }
    summary = await _run_alert_dispatch(
        evaluate_only=False,
        dispatch_only=True,
        settings=_empty_settings(),
        sinks=sinks,
    )
    assert summary["enqueued"] == 0  # Evaluator skipped.
    assert summary["delivered"] == 1
    assert len(glitchtip.calls) == 1


# ── test-alert ─────────────────────────────────────────────────────


async def test_test_alert_invokes_all_three_sinks(
    _wipe: None,
    _patch_cli_engine: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``audit test-alert --sink all`` → one synthetic call per sink."""
    glitchtip = _RecordingSink()
    email = _RecordingSink()
    slack = _RecordingSink()
    sinks: dict[str, Sink] = {
        "glitchtip": glitchtip,
        "email": email,
        "slack": slack,
    }
    summary = await _run_test_alert(
        severity="critical",
        sink_filter="all",
        settings=_empty_settings(),
        sinks=sinks,
    )
    assert summary["delivered"] == 3
    assert summary["suppressed"] == 0
    assert summary["failed"] == 0
    for s in (glitchtip, email, slack):
        assert len(s.calls) == 1
        payload, severity, rule_name = s.calls[0]
        assert severity == "critical"
        assert rule_name == "test_alert"
        assert payload["synthetic"] is True
    # An audit.event marker is recorded for the smoke test.
    async with session_factory() as s_session:
        markers = (
            await s_session.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.event "
                    "WHERE event_type = 'test_alert_emitted' "
                    "  AND severity = 'critical'"
                )
            )
        ).scalar_one()
    assert markers >= 1


async def test_test_alert_single_sink_filter(
    _wipe: None,
    _patch_cli_engine: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    glitchtip = _RecordingSink()
    email = _RecordingSink()
    slack = _RecordingSink()
    sinks: dict[str, Sink] = {
        "glitchtip": glitchtip,
        "email": email,
        "slack": slack,
    }
    summary = await _run_test_alert(
        severity="warn",
        sink_filter="slack",
        settings=_empty_settings(),
        sinks=sinks,
    )
    assert summary["delivered"] == 1
    assert glitchtip.calls == []
    assert email.calls == []
    assert len(slack.calls) == 1


async def test_test_alert_counts_suppressed_and_failed(
    _wipe: None,
    _patch_cli_engine: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sink raising SinkNotConfigured → suppressed; generic exception →
    failed; success → delivered. The CLI summary tracks each."""

    class _RaisingSink:
        async def deliver(
            self,
            payload: dict[str, object],
            severity: str,
            rule_name: str,
        ) -> bool:
            raise RuntimeError("kaboom")

    glitchtip = _RecordingSink()
    email_sink = _SuppressedSink()
    slack_sink = _RaisingSink()
    sinks: dict[str, Sink] = {
        "glitchtip": glitchtip,
        "email": email_sink,
        "slack": slack_sink,
    }
    summary = await _run_test_alert(
        severity="info",
        sink_filter="all",
        settings=_empty_settings(),
        sinks=sinks,
    )
    assert summary["delivered"] == 1
    assert summary["suppressed"] == 1
    assert summary["failed"] == 1
