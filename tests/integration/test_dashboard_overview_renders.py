"""Overview page renders against a live testcontainer DB + Redis.

Spec §8.2: GET ``/`` returns 200 with the headline counters
projected from ``OverviewVM``. The rendered HTML contains the
section titles (Outbox, Streams, Deadletter, Ingestion, Audit,
Redactions), the base template's sidebar nav, and the static-asset
references — proving the full render(request, vm) path works
end-to-end through the FastHTML app.

Configures the app with the testcontainer ``session_factory`` +
``redis_client`` fixtures. Real-role smoke runs in
``test_dashboard_queries_run_under_aslan_dashboard.py``; this test
focuses on render correctness, not privilege boundary.
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


@pytest.mark.asyncio(loop_scope="session")
async def test_overview_page_returns_200(
    _configured_dashboard: None,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio(loop_scope="session")
async def test_overview_page_renders_all_six_cards(
    _configured_dashboard: None,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/")
    body = response.text
    for card_title in (
        "Outbox",
        "Streams",
        "Deadletter",
        "Ingestion",
        "Audit",
        "Redactions",
    ):
        assert card_title in body, f"missing card title {card_title!r}"


@pytest.mark.asyncio(loop_scope="session")
async def test_overview_page_includes_base_template(
    _configured_dashboard: None,
) -> None:
    """The render helper wraps every page in the base template —
    sidebar nav + static-asset references. Catches a future
    refactor that bypasses the helper."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/")
    body = response.text
    assert "Overview · aslan dashboard" in body
    assert "/static/dashboard.css" in body
    assert "/static/htmx.min.js" in body
    assert 'href="/audit"' in body, "sidebar nav must link every page"


@pytest.mark.asyncio(loop_scope="session")
async def test_overview_page_reflects_seeded_outbox_pending_count(
    _configured_dashboard: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seed an outbox row with ``published_at IS NULL``; the page's
    Outbox card must show the seeded count."""
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
                "VALUES('kap', 'overview-render-test', 'succeeded') "
                "RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    await session.execute(
        text(
            "INSERT INTO streams.outbox "
            "(stream_name, event_id, schema_version, payload, "
            " producer_run_id, source_id) "
            "VALUES('kap', gen_random_uuid(), 1, '{}'::jsonb, :run, 'kap')"
        ),
        {"run": run_id},
    )
    await session.commit()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/")
        assert "Pending: 1" in response.text
    finally:
        async with session_factory() as s:
            await s.execute(text("DELETE FROM streams.outbox"))
            await s.execute(
                text("DELETE FROM src.ingestion_run WHERE ingestion_run_id = :rid"),
                {"rid": run_id},
            )
            await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_overview_page_is_get_only(
    _configured_dashboard: None,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/")
    assert response.status_code == 405
