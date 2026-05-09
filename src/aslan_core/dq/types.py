"""Shared dataclasses + enums for the dq subsystem.

The types here are the public boundary shapes — what callers see
when they receive a result from `dq.validation.check()` or pass
into `dq.event.emit()`. Internal SQL parameter dicts are NOT here;
they live in `_sql.py` to keep this module import-light.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class SyncRunStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    """One validation rule firing.

    The dataclass is the *return* shape of `dq.validation.check()`;
    each instance is also persisted to `audit.validation_failure` by
    the same call. Returning the list lets the caller decide whether
    to also quarantine the record per the puller's existing rules.
    """

    source: str
    rule_name: str
    severity: Severity
    record_table: str
    record_pk: dict[str, Any]
    detail: dict[str, Any]
    detected_at: str | None = None  # ISO-8601 UTC; default = inserted at now()
    failure_id: int | None = None  # populated by .check() after INSERT


@dataclass(frozen=True, slots=True)
class SpotCheckSample:
    """One pending or labelled row from `audit.spot_check_sample`.

    `record_pk` is the primary-key shape of the source row (e.g.
    `{"disclosure_id": 12345}` for KAP), as deserialised JSON.
    """

    sample_id: UUID
    source: str
    drawn_at: datetime
    record_table: str
    record_pk: dict[str, Any]
    stratum: str | None
    labelled: bool
    labelled_at: datetime | None
    labeller: str | None
    recorded_at: datetime
