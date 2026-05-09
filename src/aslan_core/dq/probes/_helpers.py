"""Shared helpers used across the per-source probes."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq.probes._sql import TABLE_EXISTS


async def table_present(session: AsyncSession, schema: str, table: str) -> bool:
    """True iff `<schema>.<table>` exists in `information_schema.tables`.

    Used by every probe's `db_latest` to gate the `MAX(...)` query —
    a source that hasn't been deployed yet has no primary table, and
    `MAX(...)` would raise `UndefinedTableError`. We catch the gap
    upstream and surface it as `(None, table_present=False)` instead.
    """
    row = (await session.execute(TABLE_EXISTS, {"schema": schema, "table": table})).one()
    return bool(row.present)


def absent_detail(schema: str, table: str) -> dict[str, Any]:
    """Probe-detail payload for an absent primary table.

    The cron writes this onto `audit.recency_observation.probe_detail`
    so operators inspecting the dashboard see why the row is "stale"
    even though no upstream/DB clock skew is responsible.
    """
    return {
        "table_present": False,
        "schema": schema,
        "table": table,
        "reason": "primary table not deployed; puller not yet wired",
    }
