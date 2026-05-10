"""MKK recency probe.

Source: MKK (Merkezi Kayıt Kuruluşu) — Turkey's central securities
depository. Primary table: ``mkk.capital_action``. Recency
dimension: ``event_at_to_db`` (gap between corporate-action effective
date and ingest).

``db_latest``: ``MAX(event_at) FROM mkk.capital_action``, gated on
table presence.

``upstream_latest``: TWO modes (mirrors the KAP probe contract):

  1. **DB-only mode** (default — ``Settings.dq_mkk_api_url is None``
     OR ``Settings.dq_mkk_api_key is None``): returns
     ``MAX(event_at)`` for both upstream and db_latest.
     Conservative — measures "time since our last ingest" rather
     than "time since MKK published" — but safe.

  2. **HTTP API mode** (opt-in via both ``DQ_MKK_API_URL`` and
     ``DQ_MKK_API_KEY``): GETs the MKK VYK endpoint with the
     ``X-API-Key`` auth header through the proxy-aware httpx
     helper. Parses the response for the latest event timestamp.
     On error: emits ``mkk_api_probe_error`` audit event and falls
     back to DB-only mode for this sweep.

The crawl repo's ``vyk.client.EntityRegistryClient`` (commit
``aae1dfe``) targets a similar VYK endpoint but the MKK recency
endpoint shape differs from the entity registry's. Rather than
import the crawl client (heavy: requires Settings, lifecycle,
its own httpx client) we inline a minimal GET via the same
proxy-aware httpx helper used by the KAP probe. M1.2 can revisit if
sidar wants the registry client reused.
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
from aslan_core.dq.probes._sql import MAX_MKK_EVENT_AT
from aslan_core.dq.types import Severity

_log = structlog.get_logger(__name__)


# Same timestamp formats as KAP — MKK publishes Turkish-locale ISO
# strings; we accept multiple shapes to absorb minor drift.
_MKK_TS_FORMATS: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _parse_mkk_ts(raw: str) -> datetime | None:
    for fmt in _MKK_TS_FORMATS:
        try:
            # Same pattern as kap.py — DTZ007 suppressed because the
            # post-parse .replace(tzinfo=UTC) is unconditional.
            dt = datetime.strptime(raw, fmt)  # noqa: DTZ007
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    return None


def _parse_mkk_payload(payload: Any) -> datetime | None:
    """Extract MAX(eventAt) from an MKK API payload.

    Accepts a top-level list ``[{"eventAt": "..."}]`` or an envelope
    ``{"events": [...]}`` / ``{"data": [...]}``. Returns None if the
    shape is unrecognised — caller falls back to DB-only mode.
    """
    if isinstance(payload, dict):
        rows = payload.get("events") or payload.get("data") or payload.get("items")
    else:
        rows = payload
    if not isinstance(rows, list):
        return None
    candidates: list[datetime] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get("eventAt") or row.get("event_at") or row.get("eventDate")
        if not isinstance(raw, str):
            continue
        ts = _parse_mkk_ts(raw)
        if ts is not None:
            candidates.append(ts)
    return max(candidates) if candidates else None


class MkkProbe:
    """Recency probe for the MKK capital-action source."""

    source: str = "mkk"

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings

    def _get_settings(self) -> Settings:
        return self._settings if self._settings is not None else Settings()

    async def upstream_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        _ = dimension
        s = self._get_settings()
        api_url = s.dq_mkk_api_url
        api_key = s.dq_mkk_api_key
        if api_url is None or api_key is None:
            return await self._db_only_upstream(session, mode="db_only_default")
        return await self._http_upstream(session, api_url, api_key.get_secret_value())

    async def _db_only_upstream(
        self, session: AsyncSession, *, mode: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        if not await table_present(session, "mkk", "capital_action"):
            return None, absent_detail("mkk", "capital_action")
        row = (await session.execute(MAX_MKK_EVENT_AT)).one()
        ts: datetime | None = row.db_latest_at
        return ts, {
            "probe": "db_only",
            "mode": mode,
            "table": "mkk.capital_action",
            "table_present": True,
        }

    async def _http_upstream(
        self, session: AsyncSession, api_url: str, api_key: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        """HTTP API mode: GET endpoint with X-API-Key, parse latest eventAt."""
        try:
            async with proxy_aware_client() as (client, proxy_label):
                resp = await client.get(
                    api_url, headers={"X-API-Key": api_key, "Accept": "application/json"}
                )
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            await dq_event.emit(
                session=session,
                event_type="mkk_api_probe_error",
                emitter="dq.probes.mkk",
                severity=Severity.WARN,
                payload={
                    "api_url": api_url,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "fallback": "db_only",
                },
            )
            ts, detail = await self._db_only_upstream(session, mode="http_error_fallback")
            detail["http_error"] = str(exc)
            detail["http_error_type"] = type(exc).__name__
            return ts, detail

        ts = _parse_mkk_payload(payload)
        if ts is None:
            await dq_event.emit(
                session=session,
                event_type="mkk_api_probe_error",
                emitter="dq.probes.mkk",
                severity=Severity.WARN,
                payload={
                    "api_url": api_url,
                    "error": "parse: no eventAt found",
                    "fallback": "db_only",
                },
            )
            db_ts, detail = await self._db_only_upstream(session, mode="http_parse_fallback")
            detail["http_parse_error"] = "no eventAt found"
            return db_ts, detail

        return ts, {
            "probe": "http_api",
            "api_url": api_url,
            "proxy": proxy_label,
        }

    async def db_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        _ = dimension
        if not await table_present(session, "mkk", "capital_action"):
            return None, absent_detail("mkk", "capital_action")
        row = (await session.execute(MAX_MKK_EVENT_AT)).one()
        ts: datetime | None = row.db_latest_at
        return ts, {"table": "mkk.capital_action", "table_present": True}
