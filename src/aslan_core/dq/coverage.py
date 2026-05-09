"""dq.coverage — point-in-time roster snapshots.

Caller pattern:

    snapshot_id = await coverage.snapshot(
        session=s,
        source="bist",
        dimension="entity",
        observed_at=now_iso,
        expected_count=502,
        actual_count=500,
        missing_ids={"entities": ["X"]},
    )

Returns the new snapshot_id, or None if (source, dimension,
observed_at) already exists (ON CONFLICT DO NOTHING — caller is
typically a cron, so a duplicate sweep at the same instant
should be idempotent).

Datetime binding: asyncpg refuses string inputs for TIMESTAMPTZ
columns, so we parse `observed_at` from ISO-8601 to a tz-aware
datetime here. Callers may pass the canonical string form for
convenience (matches the surface used elsewhere in dq).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq._sql import INSERT_COVERAGE_SNAPSHOT


def _parse_iso(iso: str) -> datetime:
    """Parse an ISO-8601 UTC string into a tz-aware datetime.

    ``Z`` suffix is normalized to ``+00:00`` for ``fromisoformat``.
    """
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso)


async def snapshot(
    *,
    session: AsyncSession,
    source: str,
    dimension: str,
    observed_at: str,
    expected_count: int | None,
    actual_count: int | None,
    missing_ids: dict[str, Any] | None = None,
    target_pct: float = 100.0,
) -> int | None:
    """Insert a coverage snapshot row. Returns snapshot_id or None on conflict."""
    result = await session.execute(
        INSERT_COVERAGE_SNAPSHOT,
        {
            "source": source,
            "dimension": dimension,
            "observed_at": _parse_iso(observed_at),
            "expected_count": expected_count,
            "actual_count": actual_count,
            "missing_ids": json.dumps(missing_ids) if missing_ids is not None else None,
            "target_pct": target_pct,
        },
    )
    snapshot_id_obj = result.scalar()
    if snapshot_id_obj is None:
        return None
    return int(snapshot_id_obj)
