"""MKK recency probe.

Source: MKK (Merkezi Kayıt Kuruluşu) — Turkey's central securities
depository. Primary table: `mkk.capital_action`. Recency dimension:
`event_at_to_db` (gap between corporate-action effective date and
ingest).

`db_latest`: `MAX(event_at) FROM mkk.capital_action`, gated on table
presence.

`upstream_latest`: placeholder for v1 — returns `now() - 1 day`. MKK
publishes capital actions with up to 1-day delay relative to the
event date; the real upstream probe queries the MKK web API.
Marked `# TODO(M1.1)`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq.probes._helpers import absent_detail, table_present
from aslan_core.dq.probes._sql import MAX_MKK_EVENT_AT


class MkkProbe:
    """Recency probe for the MKK capital-action source."""

    source: str = "mkk"

    async def upstream_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        # TODO(M1.1): replace with the real MKK API query. v1 uses
        # `now() - 1 day` as a conservative upper bound — MKK
        # publishes capital actions with up to 1-day delay.
        _ = session, dimension
        upstream = datetime.now(UTC) - timedelta(days=1)
        return upstream, {
            "probe": "placeholder",
            "offset": "1 day",
            "todo": "M1.1: query MKK capital-actions API",
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
