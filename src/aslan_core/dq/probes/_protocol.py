"""Protocol shared by all per-source recency probes.

Lives in a leaf module (rather than `__init__.py`) so concrete probe
modules can import it without forming a circular import with the
package-level re-exports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One probe's output for a single (source, dimension) pair.

    `upstream_latest_at` and `db_latest_at` may both be `None` when
    the source is not yet deployed (no upstream calendar, no primary
    table). In that case the cron skips the INSERT into
    `audit.recency_observation` (the table requires both columns
    NOT NULL via generated lag column) and records the gap in
    `probe_detail` only — the recency_observation row is omitted but
    an `audit.event(event_type='recency_probe_skipped')` is emitted
    so operators see the gap in the dashboard.

    `probe_detail` carries free-form structured metadata: which
    placeholder generated the timestamp, the upstream calendar slot
    consulted, the table-presence verdict, etc. Persisted as JSONB
    on `audit.recency_observation.probe_detail`.
    """

    upstream_latest_at: datetime | None
    db_latest_at: datetime | None
    probe_detail: dict[str, Any]


@runtime_checkable
class Probe(Protocol):
    """Per-source recency probe.

    Each concrete probe is stateless — the source name is a class
    attribute and every method takes the AsyncSession + dimension at
    call time so a single instance can serve every dimension the cron
    iterates.
    """

    source: str

    async def upstream_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        """Return `(timestamp, detail)` for the freshest upstream record.

        The detail dict is merged into `ProbeResult.probe_detail` and
        persisted on `audit.recency_observation.probe_detail`. Returns
        `(None, detail)` if the upstream is not consultable for this
        dimension (e.g. EVDS has no calendar entry yet).
        """
        ...

    async def db_latest(
        self, session: AsyncSession, dimension: str
    ) -> tuple[datetime | None, dict[str, Any]]:
        """Return `(timestamp, detail)` for the freshest DB record.

        Returns `(None, {...})` with `table_present=False` if the
        source's primary table is not in `information_schema.tables`
        (puller not yet deployed). The cron treats that as a "gap"
        signal — the source is enrolled in the SLA but not yet
        feeding data.
        """
        ...
