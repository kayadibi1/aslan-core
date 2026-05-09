"""Integration tests for /dq/scorecard — GET render + email preview + export.

Three scenarios:

  * No scorecard rows yet → 200 with the "run aslan-core audit
    scorecard" empty-state prompt.
  * Seeded scorecard_snapshot rows → 200 with the metric table, the
    summary line, and the email preview / HTML export action links.
  * /dq/scorecard/email returns the live-rendered fallback when no
    scorecard_generated event exists, and serves the persisted
    payload.body_html when it does.
  * /dq/scorecard/export downloads a standalone HTML file with the
    Content-Disposition attachment header.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import aslan_core.dashboard.pages.dq_scorecard as _dq_scorecard  # noqa: F401
from aslan_core.dashboard.app import app, configure_app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


_TEST_WEEK = date(2026, 4, 27)


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


async def _wipe(s: AsyncSession) -> None:
    await s.execute(text("DELETE FROM audit.scorecard_snapshot"))
    await s.execute(text("DELETE FROM audit.event WHERE event_type = 'scorecard_generated'"))


async def test_dq_scorecard_renders_empty_state(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await _wipe(s)
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/scorecard")
    assert resp.status_code == 200
    body = resp.text
    assert "Data Quality — Scorecard" in body
    assert "aslan-core audit scorecard" in body


async def test_dq_scorecard_renders_seeded_rows(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await _wipe(s)
        await s.execute(
            text(
                "INSERT INTO audit.scorecard_snapshot("
                "  week_start, metric_name, target, actual, status, notes"
                ") VALUES "
                "  (:w, 'kap_recency_p95', '<= 300s', '120s', 'pass', 'n=42'), "
                "  (:w, 'kap_recency_p99', '<= 1800s', '5000s', 'fail', 'n=42'), "
                "  (:w, 'bist_entity_coverage', '100%', '99.5%', 'warn', NULL)"
            ),
            {"w": _TEST_WEEK},
        )
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/scorecard")
    assert resp.status_code == 200
    body = resp.text
    # Summary line + table
    assert "Current week" in body
    assert _TEST_WEEK.isoformat() in body
    assert "kap_recency_p95" in body
    assert "PASS" in body
    assert "FAIL" in body
    assert "WARN" in body
    # Action links
    assert "/dq/scorecard/email" in body
    assert "/dq/scorecard/export" in body
    async with session_factory() as s:
        await _wipe(s)
        await s.commit()


async def test_dq_scorecard_email_preview_falls_back_when_no_event(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without a scorecard_generated event, /dq/scorecard/email
    live-renders from the snapshot table."""
    async with session_factory() as s:
        await _wipe(s)
        await s.execute(
            text(
                "INSERT INTO audit.scorecard_snapshot("
                "  week_start, metric_name, target, actual, status, notes"
                ") VALUES (:w, 'kap_recency_p95', '<= 300s', '120s', 'pass', NULL)"
            ),
            {"w": _TEST_WEEK},
        )
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/scorecard/email")
    assert resp.status_code == 200
    assert "kap_recency_p95" in resp.text
    assert "Aslan weekly scorecard" in resp.text
    async with session_factory() as s:
        await _wipe(s)
        await s.commit()


async def test_dq_scorecard_email_preview_serves_event_body(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When a scorecard_generated event has body_html in payload,
    /dq/scorecard/email serves it verbatim."""
    async with session_factory() as s:
        await _wipe(s)
        # Seed a row so the empty-state branch isn't hit (defensive).
        await s.execute(
            text(
                "INSERT INTO audit.scorecard_snapshot("
                "  week_start, metric_name, target, actual, status, notes"
                ") VALUES (:w, 'kap_recency_p95', '<= 300s', '120s', 'pass', NULL)"
            ),
            {"w": _TEST_WEEK},
        )
        await s.execute(
            text(
                "INSERT INTO audit.event("
                "  event_type, emitter, severity, payload, emitted_at"
                ") VALUES ("
                "  'scorecard_generated', 'cli:audit-scorecard', 'info', "
                "  CAST(:p AS JSONB), :t"
                ")"
            ),
            {
                "p": '{"body_html": "<html><body>SENTINEL_BODY</body></html>",'
                ' "subject": "[ASLAN AUDIT] Weekly scorecard — week of 2026-04-27"}',
                "t": datetime.now(UTC),
            },
        )
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/scorecard/email")
    assert resp.status_code == 200
    assert "SENTINEL_BODY" in resp.text
    async with session_factory() as s:
        await _wipe(s)
        await s.commit()


async def test_dq_scorecard_export_serves_attachment(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """/dq/scorecard/export serves a downloadable HTML file."""
    async with session_factory() as s:
        await _wipe(s)
        await s.execute(
            text(
                "INSERT INTO audit.scorecard_snapshot("
                "  week_start, metric_name, target, actual, status, notes"
                ") VALUES (:w, 'kap_recency_p95', '<= 300s', '120s', 'pass', NULL)"
            ),
            {"w": _TEST_WEEK},
        )
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/scorecard/export")
    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert _TEST_WEEK.isoformat() in resp.headers.get("content-disposition", "")
    body = resp.text
    assert body.startswith("<!doctype html>")
    assert "kap_recency_p95" in body
    async with session_factory() as s:
        await _wipe(s)
        await s.commit()
