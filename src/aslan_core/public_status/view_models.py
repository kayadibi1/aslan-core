"""Frozen view models for the public ``/status`` page.

Mirrors :mod:`aslan_core.dashboard.view_models` discipline:
``ConfigDict(extra='forbid', frozen=True)`` so an unexpected key from
a future query raises ``ValidationError``, and post-construction
mutation raises (a render bug that swaps in an internal/private
field fails loudly).

Only public-safe primitives are surfaced. The page intentionally does
NOT expose:

  * raw lag in seconds (operators-only — public sees a freshness %);
  * source URLs / probe internals;
  * sample IDs / record PKs / labels;
  * any field that would require joining to internal tables.

The schema is forward-compatible: when paying users justify deeper
trust commitments (incident history, subscriptions) the new fields
land on these VMs without DB changes.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class _PublicVMBase(BaseModel):
    """Shared base — frozen + extra-forbid."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StatusBadge(StrEnum):
    """Closed enum of public-facing per-source status badges.

    Mapped from SLA breach + observation freshness:

      * OK — most-recent observation is within SLA AND was recorded
        within the page's freshness window.
      * WARN — observation lag exceeds SLA but is within 2× SLA, or
        the most-recent observation is stale (>15min old).
      * CRIT — observation lag exceeds 2× SLA, or no observation
        exists at all (the cron has never run for this source).
      * UNKNOWN — sentinel for sources that exist in our coverage
        domain but have no recency_observation row yet.
    """

    OK = "ok"
    WARN = "warn"
    CRIT = "crit"
    UNKNOWN = "unknown"


class PublicSourceRowVM(_PublicVMBase):
    """One row in the public status page.

    ``last_update_at`` is the ``upstream_latest_at`` from the
    most-recent ``audit.recency_observation`` for this source — i.e.
    the most-recent upstream timestamp our system has ingested. Public
    callers care about "when was the last filing" not "what was the
    DB lag at observe-time."

    ``last_update_age_label`` is a human-readable rendering of
    ``now - last_update_at`` ("2 min ago", "3 hr ago", etc.) that the
    page renders without any additional Python logic.

    ``freshness_pct`` is a derived score in [0, 100]:
        * 100 if ``lag_seconds <= sla_target_seconds``
        * Linearly degrades to 0 as lag approaches ``2 * sla``.
    Avoids surfacing raw lag-in-seconds (operator detail) while still
    giving a meaningful number for the customer-facing summary.

    ``coverage_pct`` is the most-recent ``coverage_snapshot.coverage_pct``
    for this source (any dimension; we pick the freshest). ``None``
    if no snapshot exists.
    """

    source: str
    last_update_at: datetime | None
    last_update_age_label: str
    freshness_pct: float
    coverage_pct: float | None
    badge: StatusBadge


class PublicStatusVM(_PublicVMBase):
    """Top-level view model for the public ``/status`` page.

    ``overall_freshness_pct`` is the unweighted average of every
    source's ``freshness_pct``. Sources with no observation are
    excluded from the average so a never-observed source does not
    drag the headline to 0; the per-source badge still surfaces the
    UNKNOWN state.

    ``last_updated_at`` is the most-recent ``observed_at`` across all
    sources — i.e. when the cron last wrote a recency observation.
    The page footer renders this so customers can tell whether the
    numbers themselves are fresh.
    """

    overall_freshness_pct: float
    last_updated_at: datetime | None
    rows: list[PublicSourceRowVM]


__all__ = [
    "PublicSourceRowVM",
    "PublicStatusVM",
    "StatusBadge",
    "_PublicVMBase",
]
