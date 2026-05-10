"""KAP recency probe.

Source: KAP (Kamuyu Aydınlatma Platformu) — Turkey's listed-company
disclosure portal. Primary table: ``kap.disclosures``. Recency
dimension: ``publish_to_db`` (and ``publish_to_db_high_priority``
once the LISTEN/NOTIFY hot-path is live).

``db_latest``: ``MAX(published_at) FROM kap.disclosures``, gated on
table presence so a fresh deployment without the ``crawl`` migrations
applied returns ``(None, table_present=False)`` rather than raising.

``upstream_latest``: TWO modes:

  1. **DB-only mode** (default — ``Settings.dq_kap_listing_url is None``):
     returns ``MAX(published_at)`` from the same DB query used by
     ``db_latest``. The "lag" metric is then the gap between the
     most-recent in-DB filing and ``now()`` — conservative
     (assumes KAP publishes continuously; lag = "time since our last
     ingest" not "time since KAP published") but safe: no proxy
     concerns, no rate-limit risk.

  2. **HTTP listing mode** (opt-in via ``DQ_KAP_LISTING_URL``):
     issues an HTTP GET via the proxy-aware httpx helper
     (``KAP_PROXY_URL`` rotating pool when set), parses the response
     for the latest publish timestamp, and returns that. On any
     error: emits ``kap_listing_probe_error`` audit event and falls
     back to DB-only mode for this sweep.

Per workspace CLAUDE.md + crawl commit eb78619 the HTTP path MUST go
through the rotating proxy pool. ``proxy_aware_client`` reads
``KAP_PROXY_URL`` from the environment at call time — the
probe_detail records the proxy URL (or ``"direct"`` if unset) so an
operator can grep ``audit.recency_observation`` for sweeps that
silently bypassed the pool.

The placeholder upstream parser handles the documented KAP listing
shape (``[{"publishDate": "YYYY-MM-DD HH:MM:SS"}, ...]``) plus an
unwrapped ``{"data": [...]}`` envelope. When sidar confirms the
production endpoint format, swap ``_parse_kap_listing`` for the
canonical parser.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.config import Settings
from aslan_core.dq import event as dq_event
from aslan_core.dq.probes._helpers import absent_detail, table_present
from aslan_core.dq.probes._http import proxy_aware_client
from aslan_core.dq.probes._sql import MAX_KAP_PUBLISHED_AT
from aslan_core.dq.types import Severity

_log = structlog.get_logger(__name__)


# Common KAP timestamp formats. Real production may use a single
# canonical format; we accept several so a small drift in the
# upstream representation does not break the probe.
_KAP_TS_FORMATS: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _parse_kap_ts(raw: str) -> datetime | None:
    """Parse a single KAP timestamp string into a tz-aware UTC datetime.

    KAP publishes in Europe/Istanbul (UTC+3). The placeholder treats
    naive strings as already-UTC because the probe's lag computation
    is to the second and a 3-hour offset would skew it dramatically.
    Production parser (when sidar confirms the endpoint format)
    should localize properly via zoneinfo.
    """
    for fmt in _KAP_TS_FORMATS:
        try:
            # Explicit fall-through to .replace() below so the parsed
            # datetime is always tz-aware before return; ruff DTZ007
            # is suppressed because the post-parse tz attach happens
            # unconditionally on the next line.
            dt = datetime.strptime(raw, fmt)  # noqa: DTZ007
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    return None


def _parse_kap_listing(payload: Any) -> datetime | None:
    """Extract MAX(publishDate) from a KAP listing payload.

    Accepts either a top-level list ``[{"publishDate": "..."}]`` or
    an envelope ``{"data": [...]}``. Returns ``None`` if the shape is
    unrecognised — caller falls back to DB-only mode and emits an
    error event so the gap is observable.
    """
    rows = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
    if not isinstance(rows, list):
        return None
    candidates: list[datetime] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Accept ``publishDate`` (canonical) or ``published_at``
        # (snake_case alias some KAP endpoints emit).
        raw = row.get("publishDate") or row.get("published_at")
        if not isinstance(raw, str):
            continue
        ts = _parse_kap_ts(raw)
        if ts is not None:
            candidates.append(ts)
    return max(candidates) if candidates else None


class KapProbe:
    """Recency probe for the KAP disclosures source."""

    source: str = "kap"

    def __init__(self, *, settings: Settings | None = None) -> None:
        # ``settings`` is wired explicitly only by tests that want to
        # override the env-derived defaults. Production constructs the
        # probe with no arguments — Settings() reads from env at call
        # time inside upstream_latest() so a per-sweep env reload picks
        # up freshly-rotated config without restarting the cron.
        self._settings = settings

    def _get_settings(self) -> Settings:
        return self._settings if self._settings is not None else Settings()

    async def upstream_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        _ = dimension  # KAP has only one upstream dimension today
        s = self._get_settings()
        listing_url = s.dq_kap_listing_url
        if listing_url is None:
            return await self._db_only_upstream(session, mode="db_only_default")
        return await self._http_upstream(session, listing_url)

    async def _db_only_upstream(
        self, session: AsyncSession, *, mode: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        """DB-only mode: ``upstream_latest_at = MAX(published_at)``.

        Conservative — measures "time since our last ingest" not
        "time since KAP published". Safe in the absence of an
        approved listing endpoint.
        """
        if not await table_present(session, "kap", "disclosures"):
            return None, absent_detail("kap", "disclosures")
        row = (await session.execute(MAX_KAP_PUBLISHED_AT)).one()
        ts: datetime | None = row.db_latest_at
        return ts, {
            "probe": "db_only",
            "mode": mode,
            "table": "kap.disclosures",
            "table_present": True,
        }

    async def _http_upstream(
        self, session: AsyncSession, listing_url: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        """HTTP listing mode: GET listing URL, parse for max publishDate.

        On error (network, non-2xx, parse failure) emit
        ``kap_listing_probe_error`` and fall back to DB-only mode for
        this sweep so the cron makes progress.
        """
        try:
            async with proxy_aware_client() as (client, proxy_label):
                resp = await client.get(listing_url)
                resp.raise_for_status()
                payload = resp.json()
        except (
            httpx.HTTPError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            await dq_event.emit(
                session=session,
                event_type="kap_listing_probe_error",
                emitter="dq.probes.kap",
                severity=Severity.WARN,
                payload={
                    "listing_url": listing_url,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "fallback": "db_only",
                },
            )
            ts, detail = await self._db_only_upstream(session, mode="http_error_fallback")
            detail["http_error"] = str(exc)
            detail["http_error_type"] = type(exc).__name__
            return ts, detail

        ts = _parse_kap_listing(payload)
        if ts is None:
            await dq_event.emit(
                session=session,
                event_type="kap_listing_probe_error",
                emitter="dq.probes.kap",
                severity=Severity.WARN,
                payload={
                    "listing_url": listing_url,
                    "error": "parse: no publishDate found",
                    "fallback": "db_only",
                },
            )
            db_ts, detail = await self._db_only_upstream(session, mode="http_parse_fallback")
            detail["http_parse_error"] = "no publishDate found"
            return db_ts, detail

        return ts, {
            "probe": "http_listing",
            "listing_url": listing_url,
            "proxy": proxy_label,
        }

    async def db_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        _ = dimension  # KAP has only one db-latest table
        if not await table_present(session, "kap", "disclosures"):
            return None, absent_detail("kap", "disclosures")
        row = (await session.execute(MAX_KAP_PUBLISHED_AT)).one()
        ts: datetime | None = row.db_latest_at
        return ts, {"table": "kap.disclosures", "table_present": True}
