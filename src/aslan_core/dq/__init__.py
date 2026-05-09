"""Operational data-quality + audit subsystem for aslan-core.

See `docs/superpowers/specs/2026-05-09-data-quality-audit-design.md` for
the design. NOT to be confused with `aslan_core.audit`, which is the
actor-attribution mutation recorder (writes `audit.events`). This
module owns the operational tables (`audit.sync_log`,
`audit.recency_observation`, `audit.coverage_snapshot`, …) that
observe ingestion freshness, accuracy, and completeness.

Public surface (M0):

  * `sync_log.run(...)` — context manager for a puller invocation
  * `validation.check(table, record)` — in-line per-record validation
  * `coverage.snapshot(...)` — point-in-time roster comparison
  * `event.emit(...)` — catch-all event stream

Forward-deferred to later milestones:

  * `spot_check.label_field(...)` (M2)
  * `regression.set_status(...)` (M5)
  * `bloomberg.record_aslan_value(...)` (M4)
"""

from __future__ import annotations

from aslan_core.dq.types import (
    Severity,
    SyncRunStatus,
    ValidationFailure,
)

__all__ = [
    "Severity",
    "SyncRunStatus",
    "ValidationFailure",
]
