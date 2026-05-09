"""dq.recency — recency-observation persistence.

Caller pattern (cron-internal):

    obs_id, breached, lag = await recency.observe(
        session=s,
        source="kap",
        observed_at=now_iso,
        upstream_latest_at=upstream_iso,
        db_latest_at=db_iso,
        sla_target_seconds=300,
        probe_detail={"probe": "placeholder", ...},
    )

Returns `(observation_id, sla_breached, lag_seconds)` from the
generated columns on `audit.recency_observation`. The cron uses the
breach + lag to decide whether to emit an
`audit.event(event_type='recency_sla_breach')` follow-up.

`audit.recency_observation` requires both `upstream_latest_at` and
`db_latest_at` NOT NULL (the generated lag column would otherwise be
ill-defined). Callers with `db_latest_at=None` (probe says "table
not deployed") or `upstream_latest_at=None` (no calendar entry)
must NOT call this function — see `recency.skip()` instead, which
emits an `audit.event(event_type='recency_probe_skipped')`.

Datetime binding: asyncpg refuses string inputs for TIMESTAMPTZ
columns, so we accept ISO strings on the public surface and parse
here. The internal interface uses `datetime` only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq._sql import INSERT_RECENCY_OBSERVATION


@dataclass(frozen=True, slots=True)
class RecencyObservation:
    """Return shape of `recency.observe()`.

    Mirrors the generated columns on `audit.recency_observation` so
    the cron does not need to round-trip a follow-up SELECT to know
    whether the row breached. `lag_seconds` may be negative when the
    DB is ahead of the upstream probe (clock skew, calendar drift).
    """

    observation_id: int
    sla_breached: bool
    lag_seconds: int


def _to_dt(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    iso = value
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso)


async def observe(
    *,
    session: AsyncSession,
    source: str,
    observed_at: datetime | str,
    upstream_latest_at: datetime | str,
    db_latest_at: datetime | str,
    sla_target_seconds: int,
    probe_detail: dict[str, Any] | None = None,
) -> RecencyObservation:
    """Insert one row into `audit.recency_observation`.

    Returns `(observation_id, sla_breached, lag_seconds)` from the
    generated columns. Caller commits the surrounding session.
    """
    result = await session.execute(
        INSERT_RECENCY_OBSERVATION,
        {
            "source": source,
            "observed_at": _to_dt(observed_at),
            "upstream_latest_at": _to_dt(upstream_latest_at),
            "db_latest_at": _to_dt(db_latest_at),
            "sla_target_seconds": sla_target_seconds,
            "probe_detail": (
                json.dumps(probe_detail, default=str) if probe_detail is not None else None
            ),
        },
    )
    row = result.one()
    return RecencyObservation(
        observation_id=int(row.observation_id),
        sla_breached=bool(row.sla_breached),
        lag_seconds=int(row.lag_seconds),
    )
