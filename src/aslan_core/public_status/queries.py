"""Static-SQL queries + per-source row computation for ``/status``.

Reads ONLY from ``audit.recency_observation`` and
``audit.coverage_snapshot``. The ``public_status_reader`` PostgreSQL
role (migration 0063) holds USAGE on schema ``audit`` and SELECT on
exactly these two tables — anything else fails at the privilege
layer with ``InsufficientPrivilegeError``.

Architecture mirrors :mod:`aslan_core.dashboard.queries` but
deliberately avoids the full static-SQL AST scanner — the package has
exactly two ``text(...)`` literals, both parameterised, both reviewable
inline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.public_status.view_models import (
    PublicSourceRowVM,
    PublicStatusVM,
    StatusBadge,
)

# Closed list of public-facing sources. Mirrors the recency_sla seed
# in migration 0054 minus the internal ``extractor`` / ``dashboard``
# rows from the sync_log.source CHECK constraint — those are not
# customer-facing data sources.
_PUBLIC_SOURCES: Final[tuple[str, ...]] = ("kap", "evds", "bist", "tefas", "mkk")

# Beyond this many seconds, the most-recent recency_observation is
# considered "stale" — the page degrades the badge to WARN even if
# the lag inside the row is within SLA. The cron writes every 5 min;
# 15 min lets two cron failures slip through before the badge flips.
_OBSERVATION_STALE_SECONDS: Final[int] = 15 * 60


def _humanize_age(delta_seconds: float) -> str:
    """Render ``now - last_update_at`` for the public status row.

    Coarse buckets — public callers do not need second-precision and
    the page caches for 60s anyway. Negative deltas (clock skew)
    are clamped to ``0 sec``.
    """
    if delta_seconds < 0:
        return "0 sec ago"
    seconds = int(delta_seconds)
    if seconds < 60:
        return f"{seconds} sec ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    return f"{days} d ago"


def _freshness_pct(lag_seconds: int, sla_target_seconds: int) -> float:
    """Derive a public-facing freshness score in [0.0, 100.0].

    ``100`` if the lag is within SLA. Linearly degrades to ``0`` as
    lag approaches ``2 * sla`` (the alert threshold per
    ``audit.recency_sla.alert_at_2x``). Beyond ``2 * sla`` the score
    saturates at 0 — public callers do not need to see "how badly
    are we breached"; the badge handles that.

    ``sla_target_seconds <= 0`` is treated as a degenerate sentinel
    (should never happen — every recency_sla row has a positive
    sla_seconds) and returns 0.0.
    """
    if sla_target_seconds <= 0:
        return 0.0
    if lag_seconds <= sla_target_seconds:
        return 100.0
    if lag_seconds >= 2 * sla_target_seconds:
        return 0.0
    # Linear interpolation between SLA (100) and 2*SLA (0).
    span = sla_target_seconds  # = 2*sla - sla
    over = lag_seconds - sla_target_seconds
    return round(100.0 * (1.0 - over / span), 2)


def _classify_badge(
    lag_seconds: int,
    sla_target_seconds: int,
    *,
    observation_age_seconds: float,
) -> StatusBadge:
    """Public-facing per-source status badge.

    Order of checks matters — operationally the worst signal wins:

      1. Lag past 2× SLA → CRIT (alert threshold per recency_sla).
      2. Lag past 1× SLA → WARN.
      3. Observation row itself is stale (cron has not written
         recently) → WARN, even if the in-row lag was within SLA.
      4. Otherwise OK.
    """
    if sla_target_seconds <= 0:
        return StatusBadge.UNKNOWN
    if lag_seconds >= 2 * sla_target_seconds:
        return StatusBadge.CRIT
    if lag_seconds > sla_target_seconds:
        return StatusBadge.WARN
    if observation_age_seconds > _OBSERVATION_STALE_SECONDS:
        return StatusBadge.WARN
    return StatusBadge.OK


async def _latest_recency_per_source(
    session: AsyncSession,
) -> dict[str, tuple[int, int, datetime, datetime]]:
    """Return ``{source: (lag_seconds, sla_target_seconds,
    upstream_latest_at, observed_at)}`` for the most-recent
    ``recency_observation`` per source. Sources without an observation
    are absent from the dict.
    """
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT ON (source) "
                "  source, lag_seconds, sla_target_seconds, "
                "  upstream_latest_at, observed_at "
                "FROM audit.recency_observation "
                "ORDER BY source, observed_at DESC"
            )
        )
    ).all()
    return {
        r.source: (
            int(r.lag_seconds),
            int(r.sla_target_seconds),
            r.upstream_latest_at,
            r.observed_at,
        )
        for r in rows
    }


async def _latest_coverage_per_source(
    session: AsyncSession,
) -> dict[str, float | None]:
    """Return ``{source: coverage_pct}`` for the most-recent
    ``coverage_snapshot`` per source (any dimension; we pick the
    freshest). ``None`` if the snapshot row's ``coverage_pct`` is
    NULL (the generated column returns NULL when ``expected_count``
    is 0).
    """
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT ON (source) "
                "  source, coverage_pct "
                "FROM audit.coverage_snapshot "
                "ORDER BY source, observed_at DESC"
            )
        )
    ).all()
    return {r.source: (float(r.coverage_pct) if r.coverage_pct is not None else None) for r in rows}


def _build_row(
    *,
    source: str,
    recency: tuple[int, int, datetime, datetime] | None,
    coverage_pct: float | None,
    now: datetime,
) -> PublicSourceRowVM:
    """Pure function: assemble one row from raw query outputs.

    Extracted so unit tests can drive the per-source logic without
    a database. The caller fixes ``now`` so age labels are deterministic.
    """
    if recency is None:
        return PublicSourceRowVM(
            source=source,
            last_update_at=None,
            last_update_age_label="never",
            freshness_pct=0.0,
            coverage_pct=coverage_pct,
            badge=StatusBadge.UNKNOWN,
        )
    lag_seconds, sla_target_seconds, upstream_latest_at, observed_at = recency
    age_seconds = (now - upstream_latest_at).total_seconds()
    obs_age_seconds = (now - observed_at).total_seconds()
    return PublicSourceRowVM(
        source=source,
        last_update_at=upstream_latest_at,
        last_update_age_label=_humanize_age(age_seconds),
        freshness_pct=_freshness_pct(lag_seconds, sla_target_seconds),
        coverage_pct=coverage_pct,
        badge=_classify_badge(
            lag_seconds,
            sla_target_seconds,
            observation_age_seconds=obs_age_seconds,
        ),
    )


def _build_status(
    *,
    recency: dict[str, tuple[int, int, datetime, datetime]],
    coverage: dict[str, float | None],
    now: datetime,
) -> PublicStatusVM:
    """Pure function: assemble the top-level view model.

    Extracted so unit tests can drive the row + headline logic without
    a database. The caller fixes ``now`` so age labels and the overall
    average are deterministic.
    """
    rows = [
        _build_row(
            source=source,
            recency=recency.get(source),
            coverage_pct=coverage.get(source),
            now=now,
        )
        for source in _PUBLIC_SOURCES
    ]
    # Headline freshness: average across sources that have an
    # observation. Sources with no observation are excluded so a
    # never-observed source does not drag the headline to 0; the
    # per-source UNKNOWN badge still surfaces the gap.
    observed_rows = [r for r in rows if r.badge != StatusBadge.UNKNOWN]
    if observed_rows:
        overall = round(sum(r.freshness_pct for r in observed_rows) / len(observed_rows), 2)
    else:
        overall = 0.0
    last_updated_at = max(
        (obs_at for _, _, _, obs_at in recency.values()),
        default=None,
    )
    return PublicStatusVM(
        overall_freshness_pct=overall,
        last_updated_at=last_updated_at,
        rows=rows,
    )


async def public_status(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> PublicStatusVM:
    """Build the public ``/status`` view model.

    ``now`` is injectable so integration tests can assert deterministic
    age labels without freezing the system clock. Production callers
    pass ``None`` and the function uses ``datetime.now(tz=UTC)``.
    """
    actual_now = now if now is not None else datetime.now(tz=UTC)
    recency = await _latest_recency_per_source(session)
    coverage = await _latest_coverage_per_source(session)
    return _build_status(recency=recency, coverage=coverage, now=actual_now)


__all__ = [
    "_PUBLIC_SOURCES",
    "_build_row",
    "_build_status",
    "_classify_badge",
    "_freshness_pct",
    "_humanize_age",
    "public_status",
]
