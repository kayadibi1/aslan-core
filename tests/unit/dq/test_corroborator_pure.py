"""Unit tests for ``aslan_core.dq.corroborator`` pure helpers.

Cover the source registry + URL builders + payload extraction. The
DB-backed lookup/fetch/refresh tests live in the integration suite
(test_corroborator_roundtrip.py).
"""

from __future__ import annotations

import pytest

from aslan_core.dq import corroborator
from aslan_core.dq.corroborator import (
    _REGISTERED_SOURCES,
    CorroboratorResult,
    _extract_payload,
    _firecrawl_via_cli,
    _firecrawl_via_sdk,
    _FirecrawlOutcome,
    is_implemented,
    registered_sources,
)


def test_registered_sources_includes_v1() -> None:
    sources = registered_sources()
    assert "investing_com" in sources
    assert "kap_ir" in sources
    assert "tradingview" in sources
    assert "earningshub" in sources


def test_is_implemented_v1() -> None:
    assert is_implemented("investing_com") is True
    assert is_implemented("kap_ir") is True
    assert is_implemented("tradingview") is False
    assert is_implemented("earningshub") is False
    assert is_implemented("unknown_source") is False


def test_investing_url_builder_lowercases() -> None:
    url = _REGISTERED_SOURCES["investing_com"].build_url("AKBNK")
    assert url == "https://www.investing.com/equities/akbnk-istanbul-stock-exchange"


def test_kap_ir_url_builder_uppercases() -> None:
    url = _REGISTERED_SOURCES["kap_ir"].build_url("akbnk")
    assert url == "https://www.kap.org.tr/tr/sirket-bilgileri/AKBNK"


def test_extract_investing_pulls_known_fields() -> None:
    md = (
        "AKBNK Akbank\n"
        "Last Price: 45.20 TL\n"
        "Market Cap: 250B\n"
        "Revenue: 80B\n"
        "P/E Ratio: 4.2\n"
        "Other stuff that should not appear\n"
    )
    payload = _extract_payload("investing_com", md)
    assert payload.get("latest_price") == "45.20 TL"
    assert payload.get("market_cap") == "250B"
    assert payload.get("revenue") == "80B"
    assert payload.get("pe_ratio") == "4.2"


def test_extract_investing_returns_empty_for_no_match() -> None:
    payload = _extract_payload("investing_com", "totally unrelated content")
    assert payload == {}


def test_extract_investing_handles_none_markdown() -> None:
    payload = _extract_payload("investing_com", None)
    assert payload == {}


def test_extract_kap_ir_preserves_turkish_text() -> None:
    """Per workspace CLAUDE.md: Turkish text is preserved verbatim,
    never auto-translated."""
    md = (
        "Şirket Adı: Akbank T.A.Ş.\n"
        "Sektör: Bankacılık\n"
        "BIST Kodu: AKBNK\n"
    )
    payload = _extract_payload("kap_ir", md)
    assert payload.get("company_name") == "Akbank T.A.Ş."
    assert payload.get("sector") == "Bankacılık"
    assert payload.get("bist_ticker") == "AKBNK"


def test_extract_payload_unknown_source_empty() -> None:
    assert _extract_payload("tradingview", "Last Price: 1.0\n") == {}


def test_corroborator_result_is_frozen() -> None:
    res = CorroboratorResult(
        source="investing_com",
        entity_ticker="AKBNK",
        fetched_at=None,  # type: ignore[arg-type]
        payload={},
        fetch_url="https://example/x",
        fetch_latency_ms=10,
        fetch_status="ok",
        error_summary=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        res.fetch_status = "error"  # type: ignore[misc]


# ── Firecrawl boundary unavailable ────────────────────────────────


def test_firecrawl_via_cli_unavailable_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(corroborator.shutil, "which", lambda _name: None)
    outcome = _firecrawl_via_cli("https://example/x")
    assert outcome.status == "error"
    assert outcome.error_summary == "firecrawl unavailable"
    assert outcome.markdown is None


def test_firecrawl_via_sdk_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the SDK is importable but no API key is set, the SDK path
    returns an 'error' outcome with a deterministic summary so the
    fall-through to the CLI is triggered."""
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    # If the firecrawl SDK is available the function will look up the
    # key; if not, it returns 'firecrawl SDK not installed'. Either
    # outcome is a fall-through, both shapes are asserted below.
    outcome = _firecrawl_via_sdk("https://example/x")
    assert outcome.status == "error"
    assert outcome.error_summary in (
        "firecrawl SDK not installed",
        "FIRECRAWL_API_KEY not set",
    )


def test_firecrawl_fetch_falls_back_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: when both SDK and CLI are unreachable, the public
    ``_firecrawl_fetch`` returns the graceful-degradation shape."""
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(corroborator.shutil, "which", lambda _name: None)
    outcome = corroborator._firecrawl_fetch("https://example/x")
    assert outcome.status == "error"
    assert outcome.markdown is None
    # The summary varies by which layer reported the failure first,
    # but it is always a short non-empty string the dashboard can render.
    assert outcome.error_summary is not None and outcome.error_summary


# ── _FirecrawlOutcome shape ───────────────────────────────────────


def test_firecrawl_outcome_dataclass_is_frozen() -> None:
    outcome = _FirecrawlOutcome(status="ok", markdown="x", error_summary=None)
    with pytest.raises((AttributeError, TypeError)):
        outcome.status = "error"  # type: ignore[misc]
