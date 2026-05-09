"""EVDS recency probe.

Source: TCMB EVDS (Elektronik Veri Dağıtım Sistemi) — Türkiye Cumhuriyet
Merkez Bankası's macro time-series API. Primary table: `evds.observation`.
Recency dimension: `release_window` (per-series scheduled release).

`db_latest`: `MAX(observation_date)::timestamptz FROM evds.observation`,
gated on table presence.

`upstream_latest`: queries `audit.evds_release_calendar` for the most
recent `expected_at` whose grace window has already elapsed
(`expected_at + grace_seconds < now()`). The calendar is seeded by
migration 0055 from `dq/data/evds_release_calendar.json` and is the
source of truth for "what should be in the DB by now." This is
real (not a placeholder) — but the calendar itself is currently a
small stub that next-session work expands.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq.probes._helpers import absent_detail, table_present
from aslan_core.dq.probes._sql import (
    LATEST_EVDS_DUE,
    MAX_EVDS_OBSERVATION_DATE,
)


class EvdsProbe:
    """Recency probe for the EVDS macro-data source."""

    source: str = "evds"

    async def upstream_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        # TODO(M1.1): the calendar seed is a small stub — next session
        # will widen it to cover every enrolled series (CPI, FX, rates,
        # bond yields). The probe logic itself is final.
        _ = dimension
        row = (await session.execute(LATEST_EVDS_DUE)).one()
        ts: datetime | None = row.expected_at
        return ts, {
            "probe": "evds_release_calendar",
            "calendar_table": "audit.evds_release_calendar",
            "todo": "M1.1: widen calendar seed to all enrolled series",
        }

    async def db_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        _ = dimension
        if not await table_present(session, "evds", "observation"):
            return None, absent_detail("evds", "observation")
        row = (await session.execute(MAX_EVDS_OBSERVATION_DATE)).one()
        ts: datetime | None = row.db_latest_at
        return ts, {"table": "evds.observation", "table_present": True}
