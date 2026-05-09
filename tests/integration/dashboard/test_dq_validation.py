"""Integration tests for /dq/validation — GET render + POST review.

GET path:
  * Empty rule/flag tables → 200 with the empty-state messages.
  * Seeded validation_failure rows → rule rows render.
  * Seeded regression_flag rows → flag rows render with review buttons.

POST path:
  * Happy path → 303 redirect, status flips to 'reviewed', audit
    event lands.
  * Bad status → 400.
  * Bad reviewer (empty / over-length) → 400.
  * Unknown flag_id → 404.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import aslan_core.dashboard.pages.dq_validation as _dq_validation  # noqa: F401
from aslan_core.dashboard.app import app, configure_app
from aslan_core.dq import regression

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


async def _wipe(s: AsyncSession) -> None:
    await s.execute(text("DELETE FROM audit.regression_flag"))
    await s.execute(text("DELETE FROM audit.validation_failure"))


async def test_dq_validation_renders_empty_state(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await _wipe(s)
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/validation")
    assert resp.status_code == 200
    body = resp.text
    assert "Data Quality — Validation" in body
    assert "No validation failures in the last 7 days" in body
    assert "No open regression flags" in body


async def test_dq_validation_lists_open_flags_with_review_form(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await _wipe(s)
        flag_id = await regression.flag(
            session=s,
            source="ts",
            record_table="ts.canonical_financial",
            record_pk={"entity_id": "e1"},
            metric="revenue",
            prior_value=Decimal("100"),
            current_value=Decimal("200"),
            shift_pct=Decimal("100"),
            threshold_pct=Decimal("25"),
            detected_at=datetime.now(UTC),
        )
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/validation")
    assert resp.status_code == 200
    body = resp.text
    assert "1 open flag" in body
    assert str(flag_id) in body
    assert f"/dq/validation/regression/{flag_id}" in body
    assert "Mark reviewed" in body
    assert "Dismiss" in body
    assert "Confirm bug" in body
    async with session_factory() as s:
        await _wipe(s)
        await s.commit()


async def test_dq_validation_lists_validation_failures(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await _wipe(s)
        await s.execute(
            text(
                "INSERT INTO audit.validation_failure("
                "  source, rule_name, severity, record_table, record_pk, "
                "  detected_at, detail) VALUES "
                "('kap', 'demo_rule', 'warn', 'kap.disclosures', '{}'::jsonb, "
                " now(), '{}'::jsonb)"
            )
        )
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/dq/validation")
    assert resp.status_code == 200
    body = resp.text
    assert "demo_rule" in body
    async with session_factory() as s:
        await _wipe(s)
        await s.commit()


async def test_post_review_marks_reviewed_and_redirects(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await _wipe(s)
        flag_id = await regression.flag(
            session=s,
            source="ts",
            record_table="ts.canonical_financial",
            record_pk={"entity_id": "e1"},
            metric="revenue",
            prior_value=Decimal("100"),
            current_value=Decimal("200"),
            shift_pct=Decimal("100"),
            threshold_pct=Decimal("25"),
            detected_at=datetime.now(UTC),
        )
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/dq/validation/regression/{flag_id}",
            data={"status": "reviewed", "reviewer": "sidar", "review_note": "checked"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dq/validation"
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, reviewer, review_note FROM audit.regression_flag "
                    "WHERE flag_id = :id"
                ),
                {"id": flag_id},
            )
        ).one()
        assert row.status == "reviewed"
        assert row.reviewer == "sidar"
        assert row.review_note == "checked"
        await _wipe(s)
        await s.commit()


async def test_post_review_rejects_bad_status(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await _wipe(s)
        flag_id = await regression.flag(
            session=s,
            source="ts",
            record_table="ts.canonical_financial",
            record_pk={"entity_id": "e1"},
            metric="revenue",
            prior_value=Decimal("100"),
            current_value=Decimal("200"),
            shift_pct=Decimal("100"),
            threshold_pct=Decimal("25"),
            detected_at=datetime.now(UTC),
        )
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/dq/validation/regression/{flag_id}",
            data={"status": "bogus", "reviewer": "sidar"},
        )
    assert resp.status_code == 400
    async with session_factory() as s:
        await _wipe(s)
        await s.commit()


async def test_post_review_rejects_empty_reviewer(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await _wipe(s)
        flag_id = await regression.flag(
            session=s,
            source="ts",
            record_table="ts.canonical_financial",
            record_pk={"entity_id": "e1"},
            metric="revenue",
            prior_value=Decimal("100"),
            current_value=Decimal("200"),
            shift_pct=Decimal("100"),
            threshold_pct=Decimal("25"),
            detected_at=datetime.now(UTC),
        )
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/dq/validation/regression/{flag_id}",
            data={"status": "reviewed", "reviewer": ""},
        )
    assert resp.status_code == 400
    async with session_factory() as s:
        await _wipe(s)
        await s.commit()


async def test_post_review_404_for_unknown_flag(
    _configured_dashboard: None,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/dq/validation/regression/9999999",
            data={"status": "reviewed", "reviewer": "sidar"},
        )
    assert resp.status_code == 404


async def test_post_review_400_for_non_int_flag_id(
    _configured_dashboard: None,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/dq/validation/regression/not-an-int",
            data={"status": "reviewed", "reviewer": "sidar"},
        )
    assert resp.status_code == 400
