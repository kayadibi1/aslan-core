"""``/metrics`` does not leak forbidden field bytes.

Spec §6.3 + §8.2: for every forbidden field listed in the data
classification, seed a row containing a unique sentinel value,
GET the page (so the request goes through the metrics middleware),
GET ``/metrics``, assert the sentinel does NOT appear in the
exposition body. A future label expansion that quietly captured
operator-supplied data — e.g. echoing a request's
``query_string`` into a label — would land sentinel bytes in the
metrics body.

The matrix here mirrors the page-level sentinel suite in
``test_dashboard_sentinel_matrix.py`` so a regression that
introduced label leakage is caught at the metrics layer too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.dashboard.app import app, configure_app

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


async def _hit_then_scrape(path: str) -> str:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.get(path)
        response = await client.get("/metrics")
    assert response.status_code == 200
    body: str = response.text
    return body


@pytest.mark.asyncio(loop_scope="session")
async def test_metrics_does_not_carry_outbox_payload_sentinel(
    _configured_dashboard: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sentinel = "metrics-payload-sentinel-1f9c"
    await session.execute(
        text(
            "INSERT INTO src.source(source_id, name, kind, license_status) "
            "VALUES('kap', 'KAP', 'scraper', 'open') ON CONFLICT DO NOTHING"
        )
    )
    run_id: int = (
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run(source_id, job_name, status) "
                "VALUES('kap', 'metrics-test', 'succeeded') "
                "RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    await session.execute(
        text(
            "INSERT INTO streams.outbox "
            "(stream_name, event_id, schema_version, payload, "
            " producer_run_id, source_id) "
            "VALUES('kap', gen_random_uuid(), 1, "
            "       jsonb_build_object('leak', CAST(:s AS text)), :r, 'kap')"
        ),
        {"s": sentinel, "r": run_id},
    )
    await session.commit()
    try:
        body = await _hit_then_scrape("/outbox")
        assert sentinel not in body
    finally:
        async with session_factory() as s:
            await s.execute(
                text("DELETE FROM streams.outbox WHERE producer_run_id = :r"),
                {"r": run_id},
            )
            await s.execute(
                text("DELETE FROM src.ingestion_run WHERE ingestion_run_id = :r"),
                {"r": run_id},
            )
            await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_metrics_does_not_carry_audit_metadata_sentinel(
    _configured_dashboard: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sentinel = "metrics-audit-metadata-sentinel-7e34"
    event_id: int = (
        await session.execute(
            text(
                "INSERT INTO audit.events "
                "(occurred_at, actor_id, actor_kind, operation, "
                " target_schema, target_table, target_pk, metadata) "
                "VALUES(now(), 'cli:test@host', 'user', 'metrics-test', "
                "       'audit', 'events', '{}'::jsonb, "
                "       jsonb_build_object('leak', CAST(:s AS text))) "
                "RETURNING event_id"
            ),
            {"s": sentinel},
        )
    ).scalar_one()
    await session.commit()
    try:
        body = await _hit_then_scrape("/audit")
        assert sentinel not in body
    finally:
        async with session_factory() as s:
            await s.execute(
                text("DELETE FROM audit.events WHERE event_id = :e"),
                {"e": event_id},
            )
            await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_metrics_does_not_echo_query_string(
    _configured_dashboard: None,
) -> None:
    """A regression that added the request's query string to a
    label would land it in the metrics body. Sentinel goes in via
    the URL, exposition must not echo it."""
    sentinel = "metrics-query-string-sentinel-8a1b"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.get(f"/?q={sentinel}")
        response = await client.get("/metrics")
    assert sentinel not in response.text


@pytest.mark.asyncio(loop_scope="session")
async def test_metrics_does_not_echo_404_path(
    _configured_dashboard: None,
) -> None:
    """A regression that labelled the counter with the raw
    request path would land 404 typos in the exposition body."""
    sentinel = "metrics-404-path-sentinel-c0fe"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.get(f"/{sentinel}")
        response = await client.get("/metrics")
    assert sentinel not in response.text
