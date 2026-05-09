"""Operational data-quality + audit subsystem for aslan-core.

See `docs/superpowers/specs/2026-05-09-data-quality-audit-design.md` for
the design. NOT to be confused with `aslan_core.audit`, which is the
actor-attribution mutation recorder (writes `audit.events`). This
module owns the operational tables (`audit.sync_log`,
`audit.recency_observation`, `audit.coverage_snapshot`, …) that
observe ingestion freshness, accuracy, and completeness.

Public surface (M0+M2):

  * `sync_log.run(...)` — context manager for a puller invocation
  * `validation.check(table, record)` — in-line per-record validation
  * `coverage.snapshot(...)` — point-in-time roster comparison
  * `event.emit(...)` — catch-all event stream
  * `spot_check.draw_sample(...)` / `label_field(...)` — manual
    spot-check workflow (M2)

Forward-deferred to later milestones:

  * `regression.set_status(...)` (M5)
  * `bloomberg.record_aslan_value(...)` (M4)
"""

from __future__ import annotations

from aslan_core.dq import (
    alert_dispatch,
    corroborator,
    coverage,
    event,
    recency,
    scorecard,
    spot_check,
    sync_log,
    validation,
)
from aslan_core.dq.types import (
    Severity,
    SpotCheckSample,
    SyncRunStatus,
    ValidationFailure,
)

__all__ = [
    "Severity",
    "SpotCheckSample",
    "SyncRunStatus",
    "ValidationFailure",
    "alert_dispatch",
    "corroborator",
    "coverage",
    "event",
    "recency",
    "scorecard",
    "spot_check",
    "sync_log",
    "validation",
]
