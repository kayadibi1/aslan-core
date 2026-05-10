"""Integration tests for the NG6 corroborator panel on the
``/dq/spot-check/<sample_id>`` page + the corroborator refresh POST.

The single external-network seam (``corroborator._firecrawl_fetch``)
is mocked across every test — no real HTTP call is made.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import aslan_core.dashboard.pages.dq_spot_check as _dq_spot_check  # noqa: F401
from aslan_core.dashboard.app import app, configure_app
from aslan_core.dq import corroborator, spot_check
from aslan_core.dq.corroborator import _FirecrawlOutcome

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


@pytest_asyncio.fixture(loop_scope="session")
async def _kap_seeded_with_ticker(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    """Seed kap.disclosures + doc.filing + ref.identifier so the
    corroborator panel can resolve a BIST ticker from the sample.

    Returns the seeded ticker."""
    ticker = "AKBNK"
    async with session_factory() as s:
        await s.execute(text("CREATE SCHEMA IF NOT EXISTS kap"))
        await s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS kap.disclosures ("
                "  disclosure_id BIGSERIAL PRIMARY KEY, "
                "  event_type TEXT NOT NULL, "
                "  published_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            )
        )
        await s.execute(text("DELETE FROM kap.disclosures"))
        # The integration test container may already carry an
        # ``entity_id`` in ref.entity that we can reuse; if not we
        # insert one. We use a stable disclosure_id by reading back.
        ent_row = (
            await s.execute(text("SELECT entity_id FROM ref.entity ORDER BY entity_id LIMIT 1"))
        ).one_or_none()
        if ent_row is None:
            entity_id = (
                await s.execute(
                    text(
                        "INSERT INTO ref.entity(entity_kind, name) "
                        "VALUES ('company', 'Akbank') RETURNING entity_id"
                    )
                )
            ).scalar_one()
        else:
            entity_id = ent_row.entity_id
        # Insert a KAP disclosure
        disclosure_id = (
            await s.execute(
                text(
                    "INSERT INTO kap.disclosures(event_type) "
                    "VALUES ('material_event') RETURNING disclosure_id"
                )
            )
        ).scalar_one()
        # Wire doc.filing -> entity_id with a known source_filing_ref
        await s.execute(
            text("DELETE FROM doc.filing WHERE source_filing_ref = :sfr"),
            {"sfr": str(disclosure_id)},
        )
        await s.execute(
            text(
                "INSERT INTO doc.filing("
                "  entity_id, source_id, source_filing_ref, filing_kind, "
                "  published_at"
                ") VALUES ("
                "  :ent, 'kap', :sfr, 'disclosure', now()"
                ") "
            ),
            {"ent": entity_id, "sfr": str(disclosure_id)},
        )
        # Wire ref.identifier so the corroborator can resolve the ticker.
        await s.execute(
            text(
                "DELETE FROM ref.identifier "
                "WHERE entity_id = :ent "
                "  AND identifier_type = 'bist_ticker' "
                "  AND identifier_value = :v"
            ),
            {"ent": entity_id, "v": ticker},
        )
        await s.execute(
            text(
                "INSERT INTO ref.identifier("
                "  entity_id, identifier_type, identifier_value, valid_from"
                ") VALUES ("
                "  :ent, 'bist_ticker', :v, now()"
                ")"
            ),
            {"ent": entity_id, "v": ticker},
        )
        await s.execute(text("DELETE FROM audit.spot_check_result"))
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        await s.execute(text("DELETE FROM audit.external_corroborator_cache"))
        await s.commit()
    yield ticker
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.external_corroborator_cache"))
        await s.execute(text("DELETE FROM audit.spot_check_result"))
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        await s.execute(text("DROP TABLE IF EXISTS kap.disclosures"))
        await s.execute(text("DROP SCHEMA IF EXISTS kap CASCADE"))
        await s.commit()


# ── Panel rendering ──────────────────────────────────────────────


async def test_panel_renders_with_no_cache(
    _configured_dashboard: None,
    _kap_seeded_with_ticker: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Detail page renders the corroborator section even when no
    cache rows exist — every panel shows the Refresh button."""
    async with session_factory() as s:
        ids = await spot_check.draw_sample(session=s, source="kap", n=1)
        await s.commit()
    sid = ids[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/dq/spot-check/{sid}")
    assert resp.status_code == 200
    body = resp.text
    assert "External corroborator" in body
    # NG6 Batch-3 — all 5 implemented sources render.
    assert "Corroborator — investing_com" in body
    assert "Corroborator — kap_ir" in body
    assert "Corroborator — foreks" in body
    assert "Corroborator — matriks" in body
    assert "Corroborator — finnet" in body
    # Unimplemented sources still appear with the placeholder.
    assert "Corroborator — tradingview" in body
    assert "not yet implemented" in body


async def test_panel_renders_fresh_cache(
    _configured_dashboard: None,
    _kap_seeded_with_ticker: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fresh cache row appears as state=fresh + payload table."""
    ticker = _kap_seeded_with_ticker
    async with session_factory() as s:
        ids = await spot_check.draw_sample(session=s, source="kap", n=1)
        await s.execute(
            text(
                "INSERT INTO audit.external_corroborator_cache("
                "  source, entity_ticker, fetched_at, cached_payload, "
                "  fetch_url, fetch_latency_ms, fetch_status"
                ") VALUES ("
                "  'investing_com', :t, now(), CAST(:pl AS JSONB), "
                "  'https://example/x', 1500, 'ok'"
                ")"
            ),
            {"t": ticker, "pl": '{"latest_price": "45.20"}'},
        )
        await s.commit()
    sid = ids[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/dq/spot-check/{sid}")
    assert resp.status_code == 200
    body = resp.text
    assert "State: fresh" in body
    assert "latest_price" in body
    assert "45.20" in body
    assert "1500 ms" in body


async def test_panel_renders_stale_cache(
    _configured_dashboard: None,
    _kap_seeded_with_ticker: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A row older than 24h shows state=stale."""
    ticker = _kap_seeded_with_ticker
    stale_at = datetime.now(UTC) - timedelta(hours=48)
    async with session_factory() as s:
        ids = await spot_check.draw_sample(session=s, source="kap", n=1)
        await s.execute(
            text(
                "INSERT INTO audit.external_corroborator_cache("
                "  source, entity_ticker, fetched_at, cached_payload, "
                "  fetch_url, fetch_latency_ms, fetch_status"
                ") VALUES ("
                "  'investing_com', :t, :ts, CAST('{}' AS JSONB), "
                "  'https://example/x', 100, 'ok'"
                ")"
            ),
            {"t": ticker, "ts": stale_at},
        )
        await s.commit()
    sid = ids[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/dq/spot-check/{sid}")
    assert resp.status_code == 200
    assert "stale" in resp.text


async def test_panel_renders_no_ticker_placeholder(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A non-KAP sample (or one with no ticker resolution) renders
    the "ticker unresolved" placeholder rather than crashing."""
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.spot_check_result"))
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        await s.execute(text("CREATE SCHEMA IF NOT EXISTS evds"))
        await s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS evds.observation ("
                "  series_code TEXT NOT NULL, "
                "  observation_date DATE NOT NULL, "
                "  PRIMARY KEY(series_code, observation_date)"
                ")"
            )
        )
        await s.execute(text("DELETE FROM evds.observation"))
        await s.execute(
            text(
                "INSERT INTO evds.observation(series_code, observation_date) "
                "VALUES ('TP.PR.PROD.YK01', '2026-04-01')"
            )
        )
        ids = await spot_check.draw_sample(session=s, source="evds", n=1)
        await s.commit()
    sid = ids[0]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/dq/spot-check/{sid}")
        assert resp.status_code == 200
        assert "Could not resolve a BIST ticker" in resp.text
    finally:
        async with session_factory() as s:
            await s.execute(text("DELETE FROM audit.spot_check_result"))
            await s.execute(text("DELETE FROM audit.spot_check_sample"))
            await s.execute(text("DROP TABLE IF EXISTS evds.observation"))
            await s.commit()


# ── Refresh POST ──────────────────────────────────────────────────


async def test_refresh_writes_cache_row_and_redirects(
    _configured_dashboard: None,
    _kap_seeded_with_ticker: str,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: POST refresh → 303 to per-sample page + cache row."""
    monkeypatch.setattr(
        corroborator,
        "_firecrawl_fetch",
        lambda _url: _FirecrawlOutcome(
            status="ok",
            markdown="Last Price: 99.99\n",
            error_summary=None,
        ),
    )
    async with session_factory() as s:
        ids = await spot_check.draw_sample(session=s, source="kap", n=1)
        await s.commit()
    sid = ids[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/dq/spot-check/{sid}/corroborator/investing_com/refresh")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/dq/spot-check/{sid}"
    async with session_factory() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.external_corroborator_cache "
                    "WHERE source = 'investing_com'"
                )
            )
        ).scalar_one()
    assert n >= 1


async def test_refresh_400_for_bad_uuid(_configured_dashboard: None) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/dq/spot-check/not-a-uuid/corroborator/investing_com/refresh")
    assert resp.status_code == 400


async def test_refresh_404_for_unknown_source(
    _configured_dashboard: None,
    _kap_seeded_with_ticker: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        ids = await spot_check.draw_sample(session=s, source="kap", n=1)
        await s.commit()
    sid = ids[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/dq/spot-check/{sid}/corroborator/totally_made_up/refresh")
    assert resp.status_code == 404


async def test_refresh_400_for_unimplemented_source(
    _configured_dashboard: None,
    _kap_seeded_with_ticker: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        ids = await spot_check.draw_sample(session=s, source="kap", n=1)
        await s.commit()
    sid = ids[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/dq/spot-check/{sid}/corroborator/tradingview/refresh")
    assert resp.status_code == 400


async def test_refresh_404_for_unknown_sample(
    _configured_dashboard: None,
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/dq/spot-check/00000000-0000-0000-0000-000000000000/corroborator/investing_com/refresh"
        )
    assert resp.status_code == 404


async def test_refresh_400_when_no_ticker_can_be_resolved(
    _configured_dashboard: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A sample with no resolvable ticker → 400 (the firecrawl call
    is meaningless without a ticker)."""
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.spot_check_result"))
        await s.execute(text("DELETE FROM audit.spot_check_sample"))
        await s.execute(text("CREATE SCHEMA IF NOT EXISTS evds"))
        await s.execute(
            text(
                "CREATE TABLE IF NOT EXISTS evds.observation ("
                "  series_code TEXT NOT NULL, "
                "  observation_date DATE NOT NULL, "
                "  PRIMARY KEY(series_code, observation_date)"
                ")"
            )
        )
        await s.execute(text("DELETE FROM evds.observation"))
        await s.execute(
            text(
                "INSERT INTO evds.observation(series_code, observation_date) "
                "VALUES ('X', '2026-01-01')"
            )
        )
        ids = await spot_check.draw_sample(session=s, source="evds", n=1)
        await s.commit()
    sid = ids[0]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(f"/dq/spot-check/{sid}/corroborator/investing_com/refresh")
        assert resp.status_code == 400
    finally:
        async with session_factory() as s:
            await s.execute(text("DELETE FROM audit.spot_check_result"))
            await s.execute(text("DELETE FROM audit.spot_check_sample"))
            await s.execute(text("DROP TABLE IF EXISTS evds.observation"))
            await s.commit()
