"""Deny-by-default Pydantic view models for the v0.6.0 dashboard.

Every VM uses ``ConfigDict(extra='forbid', frozen=True)`` so an
unexpected keyword raises ``ValidationError`` (a future query that
selects a forbidden column cannot leak through an unmapped key) and
post-construction mutation raises (a render-helper bug that swaps
redacted bytes back in fails loudly).

Field types are restricted to a closed allowlist enforced by
``test_dashboard_vm_field_types_are_safe.py``: primitives,
``datetime`` / ``UUID`` / ``Decimal``, ``Literal[...]`` / ``Enum``
subclasses, parameterised ``list[T]`` / ``tuple[T, ...]`` /
``dict[str, T]``, ``Union[T, ...]``, and nested VMs. ``Any`` /
``object`` / ``bytes`` / SQLAlchemy ``Row`` are explicitly forbidden
— they would let implicit ``__str__`` / ``__repr__`` carry forbidden
bytes into the rendered HTML.

Column-level alignment with spec §6.3 — every field maps to a column
the dashboard role's GRANT actually permits. Forbidden columns
(payload, last_error raw, redacted_payload, body_text, metadata,
before, after) never appear here; their derived counterparts do
(payload_size_bytes via SECURITY DEFINER, last_error_kind via Python-
side classification, metadata_key_count via SECURITY DEFINER).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _VMBase(BaseModel):
    """Shared base. Underscore prefix excludes it from the type-safety
    test's enumeration (vacuous pass anyway since it has no fields)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RedisState(StrEnum):
    """Bounded probe outcome for a single Redis stream entry. Closed
    enum so a future probe code path cannot quietly add a new state
    without a deliberate change."""

    PRESENT = "present"
    TRIMMED = "trimmed"
    MISSING_INDEX = "missing_index"
    UNKNOWN = "unknown"


# ── Compliance banner constants ────────────────────────────────────

_BANNER_TEXT = "Network-edge logging only — not compliance evidence"


# ── Overview ───────────────────────────────────────────────────────


class OverviewVM(_VMBase):
    outbox_pending: int
    outbox_oldest_age_s: float
    outbox_drained_last_15m: int
    streams_total_xlen: int
    deadletter_total: int
    deadletter_last_24h: int
    ingestion_runs_last_24h: int
    audit_events_per_min_last_60m: float
    redaction_registry_size: int
    redaction_last_at: datetime | None


# ── Outbox ─────────────────────────────────────────────────────────


class OutboxRowVM(_VMBase):
    """Outbox row projection. ``payload_size_bytes`` is intentionally
    NOT surfaced — migration 0020's plan-rounds dropped the
    ``streams.outbox_payload_size`` SECURITY DEFINER helper after
    determining that per-row payload-size adds attack surface for
    marginal operator value. Operators rely on counts and ages
    instead. If a future operator workflow needs payload-size, add
    the helper back as a follow-up migration with the same shape as
    ``audit.event_metadata_key_count``."""

    outbox_id: int
    stream_name: str
    event_id: UUID
    schema_version: int
    source_id: str
    created_at: datetime
    published_at: datetime | None
    publish_attempts: int
    last_attempt_at: datetime | None
    last_error_kind: str | None


class OutboxVM(_VMBase):
    rows: list[OutboxRowVM]
    pending_total: int


# ── Dead-letter ────────────────────────────────────────────────────


class DeadletterRowVM(_VMBase):
    failure_id: int
    event_id: UUID
    stream_name: str
    group_name: str
    consumer_name: str
    failure_count: int
    last_error_kind: str | None
    routed_at: datetime
    routed_at_redis: datetime | None
    redis_message_id: str | None
    redis_state: RedisState


class DeadletterVM(_VMBase):
    rows: list[DeadletterRowVM]
    total: int


# ── Streams ────────────────────────────────────────────────────────


class StreamRowVM(_VMBase):
    stream_name: str
    # ``xlen`` is ``None`` when the per-stream Redis probe failed
    # (timeout, breaker open, or per-page budget exhausted). The page
    # renders that as ``?`` rather than collapsing it to ``0`` —
    # ultrareview bug_003: an ``xlen=0`` cell is operationally
    # indistinguishable from a successfully empty stream and would
    # mislead operators during incident response.
    xlen: int | None
    last_entry_age_s: float | None
    pending_per_group: dict[str, int]


class StreamsVM(_VMBase):
    rows: list[StreamRowVM]
    redis_circuit_open: bool


# ── Ingestion ──────────────────────────────────────────────────────


class IngestionRowVM(_VMBase):
    ingestion_run_id: int
    source_id: str
    job_name: str
    status: Literal["running", "succeeded", "failed", "cancelled"]
    started_at: datetime
    finished_at: datetime | None
    duration_s: float | None
    event_count: int | None
    last_error_kind: str | None


class IngestionVM(_VMBase):
    rows: list[IngestionRowVM]


# ── Documents ──────────────────────────────────────────────────────


class DocumentRowVM(_VMBase):
    filing_id: UUID
    source_id: str
    entity_name: str | None
    filing_kind: str
    published_at: datetime
    ingested_at: datetime
    attachment_count: int
    body_size_bytes: int | None
    redacted_at: datetime | None


class DocumentsVM(_VMBase):
    rows: list[DocumentRowVM]


# ── Timeseries ─────────────────────────────────────────────────────


class SeriesRowVM(_VMBase):
    series_id: int
    series_code: str
    source_id: str
    metric: str
    frequency: str
    last_observation_at: datetime | None
    observation_count_last_24h: int
    has_gap: bool


class TimeseriesVM(_VMBase):
    rows: list[SeriesRowVM]


# ── Audit ──────────────────────────────────────────────────────────


class AuditRowVM(_VMBase):
    """``audit.events.event_id`` is ``BIGSERIAL``, not UUID. The
    composite PK is ``(event_id, occurred_at)``."""

    event_id: int
    occurred_at: datetime
    actor_id: str
    actor_kind: Literal["user", "service", "system"]
    operation: str
    target_schema: str
    target_table: str
    client_ip_truncated: str
    metadata_key_count: int


class AuditVM(_VMBase):
    rows: list[AuditRowVM]
    compliance_banner: Literal["Network-edge logging only — not compliance evidence"] = _BANNER_TEXT  # type: ignore[assignment]


# ── Redactions ─────────────────────────────────────────────────────


class RedactionRowVM(_VMBase):
    event_id: UUID
    redaction_reason: str
    original_stream: str
    redacted_at: datetime
    redacted_payload_hash: str
    original_payload_hash: str
    redis_copies_total: int
    redis_copies_xdeled: int


class RedactionsVM(_VMBase):
    rows: list[RedactionRowVM]
    compliance_banner: Literal["Network-edge logging only — not compliance evidence"] = _BANNER_TEXT  # type: ignore[assignment]


# ── Error pages ────────────────────────────────────────────────────


class NotFoundVM(_VMBase):
    """404. No caller-supplied path interpolation — the rendered page
    contains only the static title."""

    title: Literal["Not found"] = "Not found"


class ServerErrorVM(_VMBase):
    """500. ``incident_id`` is a fresh UUID per response; the original
    exception text only goes to the structured log, never the rendered
    page."""

    title: Literal["Server error"] = "Server error"
    incident_id: UUID
