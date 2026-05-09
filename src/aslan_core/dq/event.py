"""dq.event — catch-all event stream.

For things that don't fit a typed table (regression flag fired,
severity rule edited, scorecard generated, …). Spec §5.9.

Datetime binding: asyncpg refuses string inputs for TIMESTAMPTZ
columns, so `emitted_at` is parsed from ISO-8601 to a tz-aware
datetime here. Public surface still accepts ISO strings for
caller convenience and consistency with the rest of dq.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq._sql import INSERT_EVENT
from aslan_core.dq.types import Severity


def _parse_iso(iso: str) -> datetime:
    """Parse an ISO-8601 UTC string into a tz-aware datetime.

    ``Z`` suffix is normalized to ``+00:00`` for ``fromisoformat``.
    """
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso)


async def emit(
    *,
    session: AsyncSession,
    event_type: str,
    emitter: str,
    payload: dict[str, Any],
    severity: Severity | None = None,
    emitted_at: str | None = None,
) -> int:
    """Insert one row into audit.event. Returns event_id.

    `severity=None` is preserved on the row (audit.event.severity is
    nullable — events that aren't alerting-relevant have no severity).
    """
    emitted_at_dt = _parse_iso(emitted_at) if emitted_at is not None else datetime.now(UTC)
    result = await session.execute(
        INSERT_EVENT,
        {
            "event_type": event_type,
            "emitter": emitter,
            "severity": severity.value if severity is not None else None,
            "payload": json.dumps(payload, default=str),
            "emitted_at": emitted_at_dt,
        },
    )
    return int(result.scalar_one())
