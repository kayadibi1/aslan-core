"""KAP recency probe.

Source: KAP (Kamuyu Aydınlatma Platformu) — Turkey's listed-company
disclosure portal. Primary table: `kap.disclosures`. Recency dimension:
`publish_to_db` (and `publish_to_db_high_priority` once the LISTEN/NOTIFY
hot-path is live).

`db_latest`: `MAX(published_at) FROM kap.disclosures`, gated on table
presence so a fresh deployment without the `crawl` migrations applied
returns `(None, table_present=False)` rather than raising.

`upstream_latest`: placeholder for v1 — returns `now() - 30s` so the
cron is testable without external HTTP. The real implementation
queries the KAP REST listing endpoint with appropriate rate limiting
and Turkish locale handling. Marked `# TODO(M1.1)` so the next session
knows where to wire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq.probes._helpers import absent_detail, table_present
from aslan_core.dq.probes._sql import MAX_KAP_PUBLISHED_AT


class KapProbe:
    """Recency probe for the KAP disclosures source."""

    source: str = "kap"

    async def upstream_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        # TODO(M1.1): replace placeholder with real upstream probe.
        # The real impl HTTP-queries the KAP REST listing endpoint
        # (https://www.kap.org.tr/tr/api/disclosures) with a small
        # rate-limit budget (KAP rejects >1 req/s per source IP) and
        # returns the `publishDate` of the most recent row. For v1
        # we assume the hot-path tail emits records within 30s of
        # publish, which is the workspace-CLAUDE.md hot-path target.
        _ = session, dimension  # placeholder ignores both
        upstream = datetime.now(UTC) - timedelta(seconds=30)
        return upstream, {
            "probe": "placeholder",
            "offset_seconds": 30,
            "todo": "M1.1: query KAP REST listing endpoint",
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
