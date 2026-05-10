"""Integration tests for ``aslan_core.dq.corroborator``.

Exercises ``lookup() / fetch() / refresh()`` end-to-end against a real
Postgres ``audit.external_corroborator_cache`` table. The single
external-network seam ``_firecrawl_fetch`` is mocked across every
test — no real HTTP call is made.

Cache hit / cache miss / stale-cache / refresh / fetch-failure
scenarios are each their own test so a regression lights up exactly
one row of the run summary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.dq import corroborator
from aslan_core.dq.corroborator import (
    CorroboratorResult,
    _FirecrawlOutcome,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _cache_clean(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Wipe the corroborator cache before + after each test."""
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.external_corroborator_cache"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.external_corroborator_cache"))
        await s.commit()


# ── lookup() ─────────────────────────────────────────────────────


async def test_lookup_returns_none_for_cache_miss(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        result = await corroborator.lookup(session=s, source="investing_com", entity_ticker="AKBNK")
    assert result is None


async def test_lookup_returns_recent_row(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cache hit: a row written 1 second ago is returned by lookup()."""
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO audit.external_corroborator_cache("
                "  source, entity_ticker, fetched_at, cached_payload, "
                "  fetch_url, fetch_latency_ms, fetch_status"
                ") VALUES ("
                "  'investing_com', 'AKBNK', now(), CAST(:pl AS JSONB), "
                "  'https://example/x', 1234, 'ok'"
                ")"
            ),
            {"pl": '{"latest_price": "45.20"}'},
        )
        await s.commit()
    async with session_factory() as s:
        result = await corroborator.lookup(session=s, source="investing_com", entity_ticker="AKBNK")
    assert result is not None
    assert result.source == "investing_com"
    assert result.entity_ticker == "AKBNK"
    assert result.payload == {"latest_price": "45.20"}
    assert result.fetch_status == "ok"
    assert result.fetch_latency_ms == 1234


async def test_lookup_skips_stale_row(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A row older than ``max_age_hours`` is skipped (returns None)."""
    stale_at = datetime.now(UTC) - timedelta(hours=48)
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO audit.external_corroborator_cache("
                "  source, entity_ticker, fetched_at, cached_payload, "
                "  fetch_url, fetch_latency_ms, fetch_status"
                ") VALUES ("
                "  'investing_com', 'AKBNK', :ts, CAST('{}' AS JSONB), "
                "  'https://example/x', 100, 'ok'"
                ")"
            ),
            {"ts": stale_at},
        )
        await s.commit()
    async with session_factory() as s:
        result = await corroborator.lookup(
            session=s,
            source="investing_com",
            entity_ticker="AKBNK",
            max_age_hours=24,
        )
    assert result is None


async def test_lookup_rejects_unknown_source(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        with pytest.raises(ValueError, match="unknown source"):
            await corroborator.lookup(session=s, source="totally_made_up", entity_ticker="AKBNK")


async def test_lookup_rejects_non_positive_max_age(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        with pytest.raises(ValueError, match="max_age_hours must be positive"):
            await corroborator.lookup(
                session=s,
                source="investing_com",
                entity_ticker="AKBNK",
                max_age_hours=0,
            )


# ── fetch() ──────────────────────────────────────────────────────


async def test_fetch_writes_cache_row_on_miss(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache miss → firecrawl call → row inserted with status=ok."""
    monkeypatch.setattr(
        corroborator,
        "_firecrawl_fetch",
        lambda _url: _FirecrawlOutcome(
            status="ok",
            markdown="Last Price: 45.20\nMarket Cap: 250B\nRevenue: 80B\n",
            error_summary=None,
        ),
    )
    async with session_factory() as s:
        result = await corroborator.fetch(session=s, source="investing_com", entity_ticker="AKBNK")
        await s.commit()
    assert result.fetch_status == "ok"
    assert result.payload.get("latest_price") == "45.20"
    assert result.payload.get("market_cap") == "250B"
    async with session_factory() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.external_corroborator_cache "
                    "WHERE source = 'investing_com' AND entity_ticker = 'AKBNK'"
                )
            )
        ).scalar_one()
    assert n == 1


async def test_fetch_returns_cache_hit_without_firecrawl_call(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh cache row short-circuits — _firecrawl_fetch must NOT
    be called (cost-discipline assertion)."""
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO audit.external_corroborator_cache("
                "  source, entity_ticker, fetched_at, cached_payload, "
                "  fetch_url, fetch_latency_ms, fetch_status"
                ") VALUES ("
                "  'investing_com', 'AKBNK', now(), CAST('{}' AS JSONB), "
                "  'https://example/x', 100, 'ok'"
                ")"
            )
        )
        await s.commit()

    call_count = {"n": 0}

    def _trap(_url: str) -> _FirecrawlOutcome:
        call_count["n"] += 1
        return _FirecrawlOutcome(status="ok", markdown="x", error_summary=None)

    monkeypatch.setattr(corroborator, "_firecrawl_fetch", _trap)
    async with session_factory() as s:
        result = await corroborator.fetch(session=s, source="investing_com", entity_ticker="AKBNK")
    assert isinstance(result, CorroboratorResult)
    assert call_count["n"] == 0, "fetch() called _firecrawl_fetch on a cache hit"


async def test_fetch_writes_error_row_on_failure(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_status != 'ok' still writes the cache row so the
    labeller sees the failure trail."""
    monkeypatch.setattr(
        corroborator,
        "_firecrawl_fetch",
        lambda _url: _FirecrawlOutcome(
            status="rate_limited",
            markdown=None,
            error_summary="429 too many requests",
        ),
    )
    async with session_factory() as s:
        result = await corroborator.fetch(session=s, source="investing_com", entity_ticker="AKBNK")
        await s.commit()
    assert result.fetch_status == "rate_limited"
    assert result.payload == {}
    assert result.error_summary == "429 too many requests"
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT fetch_status, error_summary "
                    "FROM audit.external_corroborator_cache "
                    "WHERE source = 'investing_com' AND entity_ticker = 'AKBNK'"
                )
            )
        ).one()
    assert row.fetch_status == "rate_limited"
    assert row.error_summary == "429 too many requests"


# ── refresh() ────────────────────────────────────────────────────


async def test_refresh_writes_new_row_even_with_fresh_cache(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """refresh() ignores the cache: a same-day cached row does NOT
    short-circuit. The cache becomes append-only proof of the fetch
    trail."""
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO audit.external_corroborator_cache("
                "  source, entity_ticker, fetched_at, cached_payload, "
                "  fetch_url, fetch_latency_ms, fetch_status"
                ") VALUES ("
                "  'investing_com', 'AKBNK', now(), CAST('{}' AS JSONB), "
                "  'https://example/x', 50, 'ok'"
                ")"
            )
        )
        await s.commit()
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
        result = await corroborator.refresh(
            session=s, source="investing_com", entity_ticker="AKBNK"
        )
        await s.commit()
    assert result.fetch_status == "ok"
    assert result.payload.get("latest_price") == "99.99"
    async with session_factory() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.external_corroborator_cache "
                    "WHERE source = 'investing_com' AND entity_ticker = 'AKBNK'"
                )
            )
        ).scalar_one()
    assert n == 2


async def test_refresh_unimplemented_raises(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            await corroborator.refresh(session=s, source="tradingview", entity_ticker="AKBNK")


async def test_refresh_unknown_source_raises(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        with pytest.raises(ValueError, match="unknown source"):
            await corroborator.refresh(session=s, source="totally_made_up", entity_ticker="AKBNK")


async def test_refresh_kap_ir_extracts_turkish_payload(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KAP IR source preserves Turkish text verbatim per workspace
    CLAUDE.md "Turkish-language fidelity"."""
    monkeypatch.setattr(
        corroborator,
        "_firecrawl_fetch",
        lambda _url: _FirecrawlOutcome(
            status="ok",
            markdown=("Şirket Adı: Akbank T.A.Ş.\nSektör: Bankacılık\nBIST Kodu: AKBNK\n"),
            error_summary=None,
        ),
    )
    async with session_factory() as s:
        result = await corroborator.refresh(session=s, source="kap_ir", entity_ticker="AKBNK")
        await s.commit()
    assert result.fetch_status == "ok"
    assert result.payload.get("company_name") == "Akbank T.A.Ş."
    assert result.payload.get("sector") == "Bankacılık"
    assert result.payload.get("bist_ticker") == "AKBNK"


# ── NG6 Batch-3 adapters ─────────────────────────────────────────


async def test_refresh_investing_com_uses_slug_map(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``audit.investing_com_slug`` carries a row for the ticker,
    refresh() builds the canonical ``tr.investing.com/equities/<slug>``
    URL rather than the legacy ``-istanbul-stock-exchange`` shape."""
    captured: dict[str, str] = {}

    def _capture(url: str) -> _FirecrawlOutcome:
        captured["url"] = url
        return _FirecrawlOutcome(status="ok", markdown="Last Price: 1.0\n", error_summary=None)

    monkeypatch.setattr(corroborator, "_firecrawl_fetch", _capture)
    async with session_factory() as s:
        await corroborator.refresh(session=s, source="investing_com", entity_ticker="AKBNK")
        await s.commit()
    assert captured["url"] == "https://tr.investing.com/equities/akbank", (
        f"expected slug-map URL, got {captured['url']}"
    )


async def test_refresh_investing_com_emits_slug_missing_event(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ticker missing from the slug map falls through to the legacy
    URL builder AND emits a ``corroborator_slug_missing`` audit event."""
    monkeypatch.setattr(
        corroborator,
        "_firecrawl_fetch",
        lambda _url: _FirecrawlOutcome(status="ok", markdown="x", error_summary=None),
    )
    async with session_factory() as s:
        before = (
            await s.execute(
                text(
                    "SELECT count(*)::int FROM audit.event "
                    "WHERE event_type = 'corroborator_slug_missing'"
                )
            )
        ).scalar_one()
    async with session_factory() as s:
        await corroborator.refresh(
            session=s, source="investing_com", entity_ticker="UNKNOWN_TICKER_XYZ"
        )
        await s.commit()
    async with session_factory() as s:
        after = (
            await s.execute(
                text(
                    "SELECT count(*)::int FROM audit.event "
                    "WHERE event_type = 'corroborator_slug_missing'"
                )
            )
        ).scalar_one()
    assert int(after) == int(before) + 1


async def test_refresh_foreks_extracts_payload(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def _capture(url: str) -> _FirecrawlOutcome:
        captured["url"] = url
        return _FirecrawlOutcome(
            status="ok",
            markdown="Son Fiyat: 45.20 TL\nPiyasa Değeri: 250B\n",
            error_summary=None,
        )

    monkeypatch.setattr(corroborator, "_firecrawl_fetch", _capture)
    async with session_factory() as s:
        result = await corroborator.refresh(session=s, source="foreks", entity_ticker="akbnk")
        await s.commit()
    assert captured["url"] == "https://www.foreks.com/borsa/hisse-detay/AKBNK"
    assert result.fetch_status == "ok"
    assert result.payload.get("latest_price") == "45.20 TL"
    assert result.payload.get("market_cap") == "250B"


async def test_refresh_matriks_extracts_payload(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def _capture(url: str) -> _FirecrawlOutcome:
        captured["url"] = url
        return _FirecrawlOutcome(
            status="ok",
            markdown="Last Price: 12.34\nMarket Cap: 99B\n",
            error_summary=None,
        )

    monkeypatch.setattr(corroborator, "_firecrawl_fetch", _capture)
    async with session_factory() as s:
        result = await corroborator.refresh(session=s, source="matriks", entity_ticker="akbnk")
        await s.commit()
    assert captured["url"] == "https://www.matriks.com.tr/teknik-analiz/AKBNK"
    assert result.fetch_status == "ok"
    assert result.payload.get("latest_price") == "12.34"


async def test_refresh_finnet_extracts_payload(
    _cache_clean: None,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def _capture(url: str) -> _FirecrawlOutcome:
        captured["url"] = url
        return _FirecrawlOutcome(
            status="ok",
            markdown="Last Price: 7.89\nRevenue: 5B\n",
            error_summary=None,
        )

    monkeypatch.setattr(corroborator, "_firecrawl_fetch", _capture)
    async with session_factory() as s:
        result = await corroborator.refresh(session=s, source="finnet", entity_ticker="akbnk")
        await s.commit()
    assert captured["url"] == "https://www.finnet.gen.tr/CompanyResearch/Equity/AKBNK"
    assert result.fetch_status == "ok"
    assert result.payload.get("latest_price") == "7.89"
    assert result.payload.get("revenue") == "5B"
