"""dq.corroborator — external-source spot-check helper (NG6).

Spec §7.2 promotes "v2 lands later" to in-scope per the workspace
CLAUDE.md autonomy directive. This module surfaces a "second opinion"
panel on the M2 spot-check UI: for each registered external source
(Investing.com Turkey, KAP IR page, ...) the labeller can compare a
fresh-extracted reference value against the canonical DB row.

Three public surfaces:

  * ``lookup(*, session, source, entity_ticker, max_age_hours=24)`` —
    cache-only read; returns the latest non-stale row from
    ``audit.external_corroborator_cache`` or ``None``.
  * ``fetch(*, session, source, entity_ticker)`` — orchestration:
    cache hit returns immediately; cache miss / stale calls firecrawl,
    extracts the small comparable payload, writes a new cache row,
    returns the result.
  * ``refresh(*, session, source, entity_ticker)`` — forces a fresh
    fetch ignoring cache, writes a new row.

Cost discipline (workspace CLAUDE.md §3): firecrawl is paid; the
24-hour TTL + manual-refresh-only design caps spend even if
labellers open hundreds of spot-check pages a week.

Audit discipline: every fetch attempt — successful or not — writes a
cache row with ``fetch_status`` ∈ {ok, error, rate_limited, blocked}
+ ``fetch_latency_ms`` so the labeller sees fetch transparency, and so
operators can audit the call trail later.

Network discipline: every external HTTP call goes through
``_firecrawl_fetch`` (see below). The AST canary in
``tests/unit/test_corroborator_no_external_io.py`` enforces that no
other module member imports requests / httpx / urllib at module level.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq._sql import (
    INSERT_CORROBORATOR_CACHE,
    SELECT_CORROBORATOR_LATEST,
)

_log = structlog.get_logger(__name__)


# ── Public dataclass ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CorroboratorResult:
    """One cache row's worth of corroborator data.

    Returned from ``lookup`` / ``fetch`` / ``refresh``. ``payload`` is
    the small extracted dict (latest_price / market_cap / revenue /
    company_name / sector / ... — varies per source). On a failed
    fetch ``fetch_status != 'ok'`` and ``payload`` is empty / partial;
    ``error_summary`` carries the first ~200 chars of the failure.
    """

    source: str
    entity_ticker: str
    fetched_at: datetime
    payload: dict[str, Any]
    fetch_url: str
    fetch_latency_ms: int
    fetch_status: str
    error_summary: str | None


# ── Source registry ──────────────────────────────────────────────


def _build_investing_com_url(entity_ticker: str) -> str:
    """Investing.com Turkey URL pattern.

    NOTE (NG6 v1): Investing.com's URL slug is *not* a deterministic
    function of the BIST ticker — the real URL is
    ``/equities/<company-slug>`` where the slug is editorial (e.g.
    AKBNK -> ``/equities/akbank``). v1 builds the search-style URL
    ``/equities/<ticker>-istanbul-stock-exchange`` which Investing's
    edge redirects to the real entity page when the slug exists.
    Production hardening (v2) replaces this with a per-ticker slug
    map maintained alongside ``ref.identifier``.
    """
    slug = entity_ticker.strip().lower()
    return f"https://www.investing.com/equities/{slug}-istanbul-stock-exchange"


def _build_kap_ir_url(entity_ticker: str) -> str:
    """KAP company IR page URL pattern.

    NG6 v1: ``/tr/sirket-bilgileri/<entity_id>`` requires the KAP
    ``entity_id`` (a numeric internal id), which is not in scope for
    every spot-check sample. Fall back to the company-list search
    URL keyed on the ticker so the labeller at least lands on the
    right entity even when the deep-link entity_id isn't resolved.
    """
    slug = entity_ticker.strip().upper()
    return f"https://www.kap.org.tr/tr/sirket-bilgileri/{slug}"


@dataclass(frozen=True, slots=True)
class _SourceHandler:
    """Per-source URL builder + extraction prompt + implemented flag.

    ``implemented=False`` sources are *registered* (so they appear in
    the dashboard's source list) but ``fetch()`` raises
    ``NotImplementedError`` until they grow a real handler.
    """

    name: str
    build_url: Any  # Callable[[str], str]; loose type to keep registry literal-friendly
    extraction_prompt: str
    implemented: bool


_INVESTING_PROMPT = (
    "Extract the following fields if present on the page, returning a JSON "
    "object with keys: latest_price (number), price_currency (string), "
    "market_cap (string), revenue (string), pe_ratio (number). Use null "
    "for any field that is not surfaced. Do not infer or compute values."
)

_KAP_IR_PROMPT = (
    "Extract the following fields if present on the page, returning a JSON "
    "object with keys: company_name (string), bist_ticker (string), "
    "sector (string), kap_entity_id (string). Use null for any field that "
    "is not surfaced. Do not infer or compute values."
)


_REGISTERED_SOURCES: dict[str, _SourceHandler] = {
    "investing_com": _SourceHandler(
        name="investing_com",
        build_url=_build_investing_com_url,
        extraction_prompt=_INVESTING_PROMPT,
        implemented=True,
    ),
    "kap_ir": _SourceHandler(
        name="kap_ir",
        build_url=_build_kap_ir_url,
        extraction_prompt=_KAP_IR_PROMPT,
        implemented=True,
    ),
    # Registered-but-not-implemented sources. The dashboard surfaces
    # them with a "not yet implemented" badge so the operator sees the
    # roadmap without each placeholder needing its own UI work.
    "tradingview": _SourceHandler(
        name="tradingview",
        build_url=lambda t: f"https://www.tradingview.com/symbols/BIST-{t.upper()}/",
        extraction_prompt="",
        implemented=False,
    ),
    "earningshub": _SourceHandler(
        name="earningshub",
        build_url=lambda t: f"https://www.earningshub.com/turkey/{t.upper()}",
        extraction_prompt="",
        implemented=False,
    ),
}


def registered_sources() -> tuple[str, ...]:
    """Return the registered source names in deterministic order.

    The dashboard renders one panel per source; the order here drives
    the rendered order so the labeller's eye-path is stable across
    page loads.
    """
    return tuple(_REGISTERED_SOURCES.keys())


def is_implemented(source: str) -> bool:
    """Whether ``source`` has a real fetch handler. Registered-but-
    unimplemented sources return ``False``; ``fetch()`` raises
    ``NotImplementedError`` for them."""
    handler = _REGISTERED_SOURCES.get(source)
    if handler is None:
        return False
    return handler.implemented


# ── Firecrawl integration boundary ────────────────────────────────


@dataclass(frozen=True, slots=True)
class _FirecrawlOutcome:
    """Internal shape of one ``_firecrawl_fetch`` call.

    Encapsulated so test code can mock the boundary without monkey-
    patching individual exceptions through subprocess / SDK call sites.
    """

    status: str  # 'ok' | 'error' | 'rate_limited' | 'blocked'
    markdown: str | None
    error_summary: str | None


_FIRECRAWL_TIMEOUT_SECONDS = 30


def _firecrawl_via_sdk(url: str) -> _FirecrawlOutcome:
    """Try to call the firecrawl Python SDK.

    The SDK is an optional dep — base installs of aslan-core do NOT
    pin it. ``ImportError`` flows back as ``status='error'`` with a
    short ``error_summary`` so the dashboard renders gracefully.
    """
    try:
        # Imported lazily so the AST canary's "no requests/httpx at
        # module level" rule has no false positives on this module.
        from firecrawl import FirecrawlApp  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        return _FirecrawlOutcome(
            status="error",
            markdown=None,
            error_summary="firecrawl SDK not installed",
        )
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return _FirecrawlOutcome(
            status="error",
            markdown=None,
            error_summary="FIRECRAWL_API_KEY not set",
        )
    try:
        app = FirecrawlApp(api_key=api_key)
        result = app.scrape_url(url, params={"formats": ["markdown"]})
    except Exception as exc:
        text = str(exc).lower()
        if "rate" in text and "limit" in text:
            status = "rate_limited"
        elif "block" in text or "forbidden" in text or "403" in text:
            status = "blocked"
        else:
            status = "error"
        return _FirecrawlOutcome(
            status=status,
            markdown=None,
            error_summary=str(exc)[:200],
        )
    md = None
    if isinstance(result, dict):
        md = result.get("markdown")
        if md is None and isinstance(result.get("data"), dict):
            md = result["data"].get("markdown")
    return _FirecrawlOutcome(status="ok", markdown=md, error_summary=None)


def _firecrawl_via_cli(url: str) -> _FirecrawlOutcome:
    """Invoke the firecrawl CLI when the SDK is unavailable.

    Resolves the executable via ``shutil.which`` (absolute path; no
    shell expansion) and bounds the call with a 30-second timeout.
    Stdout is expected to carry markdown; stderr captures error text.
    """
    exe = shutil.which("firecrawl")
    if exe is None:
        return _FirecrawlOutcome(
            status="error",
            markdown=None,
            error_summary="firecrawl unavailable",
        )
    try:
        proc = subprocess.run(  # noqa: S603 — exe resolved via shutil.which; no shell
            [exe, "scrape", url, "--format", "markdown"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_FIRECRAWL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _FirecrawlOutcome(
            status="error",
            markdown=None,
            error_summary="firecrawl CLI timed out",
        )
    except OSError as exc:
        return _FirecrawlOutcome(
            status="error",
            markdown=None,
            error_summary=f"firecrawl CLI exec failed: {exc}"[:200],
        )
    if proc.returncode != 0:
        text = (proc.stderr or "").lower()
        if "rate" in text and "limit" in text:
            status = "rate_limited"
        elif "block" in text or "forbidden" in text or "403" in text:
            status = "blocked"
        else:
            status = "error"
        return _FirecrawlOutcome(
            status=status,
            markdown=None,
            error_summary=(proc.stderr or "")[:200] or f"exit {proc.returncode}",
        )
    return _FirecrawlOutcome(status="ok", markdown=proc.stdout or None, error_summary=None)


def _firecrawl_fetch(url: str) -> _FirecrawlOutcome:
    """Single network seam — every external HTTP call goes through here.

    Tries the SDK first (faster, structured response); falls back to
    the CLI; if both are unavailable returns
    ``status='error', error_summary='firecrawl unavailable'`` so the
    dashboard renders gracefully without a crash.

    The AST canary asserts that no other corroborator code imports
    requests / httpx / urllib so this is the *only* network boundary
    in the module.
    """
    sdk_outcome = _firecrawl_via_sdk(url)
    if sdk_outcome.status == "ok":
        return sdk_outcome
    if sdk_outcome.error_summary in (
        "firecrawl SDK not installed",
        "FIRECRAWL_API_KEY not set",
    ):
        # Fall through to the CLI — the SDK simply isn't available.
        return _firecrawl_via_cli(url)
    # SDK reachable but returned an error / rate-limit / blocked — do
    # NOT retry through the CLI; that would double-charge the user
    # for a known bad fetch.
    return sdk_outcome


# ── Payload extraction ────────────────────────────────────────────


def _extract_payload(source: str, markdown: str | None) -> dict[str, Any]:
    """Pull a small comparable dict out of the markdown blob.

    Per source:
      * investing_com — looks for "Last Price", "Market Cap", "Revenue".
      * kap_ir       — looks for "Şirket Adı", "Sektör", "BIST Kodu".
    Returns an empty dict if markdown is None or the patterns don't
    match — never raises, the dashboard just renders "—" for empty
    fields.
    """
    if markdown is None:
        return {}
    if source == "investing_com":
        return _extract_investing(markdown)
    if source == "kap_ir":
        return _extract_kap_ir(markdown)
    return {}


def _extract_investing(markdown: str) -> dict[str, Any]:
    """Parse the small set of investing.com fields out of markdown.

    Conservative — every value is the raw token captured between the
    label and the first newline / pipe. The labeller compares text
    against the canonical DB row, so we DO NOT normalise units or
    parse numbers here.
    """
    out: dict[str, Any] = {}
    lower = markdown.lower()
    for label, key in (
        ("last price", "latest_price"),
        ("market cap", "market_cap"),
        ("revenue", "revenue"),
        ("p/e ratio", "pe_ratio"),
    ):
        idx = lower.find(label)
        if idx == -1:
            continue
        # Take the rest of the line after the label, then the first
        # token before any pipe / newline.
        tail = markdown[idx + len(label) :].split("\n", 1)[0]
        token = tail.strip(" :|").split("|", 1)[0].strip()
        if token:
            out[key] = token[:128]
    return out


def _extract_kap_ir(markdown: str) -> dict[str, Any]:
    """Parse company name / sector / ticker from the KAP IR page.

    The KAP page renders Turkish labels (``Şirket Adı``, ``Sektörü``,
    ``BIST Kodu``); per workspace CLAUDE.md "Turkish-language fidelity"
    we MUST NOT auto-translate — the captured value is preserved in
    Turkish on the rendered panel.
    """
    out: dict[str, Any] = {}
    lower = markdown.lower()
    for label, key in (
        ("şirket adı", "company_name"),
        ("sirket adi", "company_name"),
        ("sektör", "sector"),
        ("sektor", "sector"),
        ("bist kodu", "bist_ticker"),
        ("bist kod", "bist_ticker"),
    ):
        idx = lower.find(label)
        if idx == -1 or key in out:
            continue
        tail = markdown[idx + len(label) :].split("\n", 1)[0]
        token = tail.strip(" :|").split("|", 1)[0].strip()
        if token:
            out[key] = token[:128]
    return out


# ── Cache helpers ────────────────────────────────────────────────


def _row_to_result(row: Any) -> CorroboratorResult:
    payload = row.cached_payload
    if isinstance(payload, str):
        try:
            payload_dict: dict[str, Any] = json.loads(payload)
        except (ValueError, TypeError):
            payload_dict = {}
    elif isinstance(payload, dict):
        payload_dict = payload
    else:
        payload_dict = {}
    return CorroboratorResult(
        source=row.source,
        entity_ticker=row.entity_ticker,
        fetched_at=row.fetched_at,
        payload=payload_dict,
        fetch_url=row.fetch_url,
        fetch_latency_ms=int(row.fetch_latency_ms),
        fetch_status=row.fetch_status,
        error_summary=row.error_summary,
    )


async def _select_latest_row(
    session: AsyncSession, *, source: str, entity_ticker: str
) -> Any | None:
    """Return the latest cache row for (source, entity_ticker) or None."""
    return (
        await session.execute(
            SELECT_CORROBORATOR_LATEST,
            {"source": source, "entity_ticker": entity_ticker},
        )
    ).one_or_none()


async def _insert_cache_row(session: AsyncSession, *, result: CorroboratorResult) -> int:
    insert = await session.execute(
        INSERT_CORROBORATOR_CACHE,
        {
            "source": result.source,
            "entity_ticker": result.entity_ticker,
            "fetched_at": result.fetched_at,
            "cached_payload": json.dumps(result.payload, default=str),
            "fetch_url": result.fetch_url,
            "fetch_latency_ms": result.fetch_latency_ms,
            "fetch_status": result.fetch_status,
            "error_summary": result.error_summary,
        },
    )
    return int(insert.scalar_one())


# ── Public API ───────────────────────────────────────────────────


async def lookup(
    *,
    session: AsyncSession,
    source: str,
    entity_ticker: str,
    max_age_hours: int = 24,
) -> CorroboratorResult | None:
    """Cache-only read.

    Returns the latest ``audit.external_corroborator_cache`` row for
    ``(source, entity_ticker)`` if it was fetched within the last
    ``max_age_hours``. Returns ``None`` for cache miss or stale rows
    so the caller can decide whether to ``fetch()``.

    Raises ``ValueError`` for unknown source or non-positive
    ``max_age_hours``.
    """
    if source not in _REGISTERED_SOURCES:
        raise ValueError(
            f"unknown source {source!r}; expected one of {sorted(_REGISTERED_SOURCES)}"
        )
    if max_age_hours <= 0:
        raise ValueError(f"max_age_hours must be positive; got {max_age_hours}")
    row = await _select_latest_row(session, source=source, entity_ticker=entity_ticker)
    if row is None:
        return None
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    if row.fetched_at < cutoff:
        return None
    return _row_to_result(row)


async def fetch(
    *,
    session: AsyncSession,
    source: str,
    entity_ticker: str,
    max_age_hours: int = 24,
) -> CorroboratorResult:
    """Cache-aware fetch.

    Returns the cached row when one exists within ``max_age_hours``;
    otherwise calls ``_firecrawl_fetch``, extracts the payload, writes
    a new cache row, and returns the result. Every fetch attempt —
    successful or not — writes a cache row so the labeller sees the
    fetch trail.

    Raises ``ValueError`` for unknown source or non-positive
    ``max_age_hours``. Raises ``NotImplementedError`` when ``source``
    is registered but does not yet have an extraction handler.
    """
    if source not in _REGISTERED_SOURCES:
        raise ValueError(
            f"unknown source {source!r}; expected one of {sorted(_REGISTERED_SOURCES)}"
        )
    cached = await lookup(
        session=session,
        source=source,
        entity_ticker=entity_ticker,
        max_age_hours=max_age_hours,
    )
    if cached is not None:
        return cached
    return await refresh(session=session, source=source, entity_ticker=entity_ticker)


async def refresh(
    *,
    session: AsyncSession,
    source: str,
    entity_ticker: str,
) -> CorroboratorResult:
    """Force a fresh firecrawl fetch ignoring cache.

    Always writes a new cache row (even when status != 'ok'), so the
    cache history surfaces the failure trail.

    Raises ``ValueError`` for unknown source. Raises
    ``NotImplementedError`` for registered-but-unimplemented sources.
    """
    handler = _REGISTERED_SOURCES.get(source)
    if handler is None:
        raise ValueError(
            f"unknown source {source!r}; expected one of {sorted(_REGISTERED_SOURCES)}"
        )
    if not handler.implemented:
        raise NotImplementedError(
            f"corroborator source {source!r} is registered but not yet implemented; "
            f"see aslan_core.dq.corroborator._REGISTERED_SOURCES"
        )

    fetch_url = handler.build_url(entity_ticker)
    started = time.monotonic()
    outcome = _firecrawl_fetch(fetch_url)
    latency_ms = int((time.monotonic() - started) * 1000)
    fetched_at = datetime.now(UTC)
    payload = _extract_payload(source, outcome.markdown) if outcome.status == "ok" else {}
    result = CorroboratorResult(
        source=source,
        entity_ticker=entity_ticker,
        fetched_at=fetched_at,
        payload=payload,
        fetch_url=fetch_url,
        fetch_latency_ms=latency_ms,
        fetch_status=outcome.status,
        error_summary=outcome.error_summary,
    )
    try:
        await _insert_cache_row(session, result=result)
    except Exception as exc:
        # The fetch succeeded (or its failure is captured in result);
        # a cache-write failure should not blow the whole call up.
        # Log + return the in-memory result so the caller still sees
        # the data; the labeller will refetch on the next refresh.
        _log.warning(
            "corroborator.cache_write_failed",
            source=source,
            entity_ticker=entity_ticker,
            error=str(exc),
        )
    return result


__all__ = [
    "CorroboratorResult",
    "fetch",
    "is_implemented",
    "lookup",
    "refresh",
    "registered_sources",
]
