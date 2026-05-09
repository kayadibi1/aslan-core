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


# ── Review queues (aslan-event-extractor M0) ─────────────────────


class ReviewVM(_VMBase):
    """Top-level /review page — depth counters for the three review
    queues that aslan-event-extractor populates.

    Per aslan-event-extractor SCOPE_v2 D26 / M0: this is a skeleton —
    counters only, no per-row resolution UI. The resolution UI lands
    in the extractor's M3-M5 milestones when there's actual data to
    review and the counters justify a richer view.
    """

    review_queue_pending: int
    """Unresolved rows in agg.filing_event_review_queue (resolved_at IS NULL).
    Tier 1/2 disagreements awaiting human resolution per SCOPE_v2 D9."""

    entity_resolution_pending: int
    """Unresolved rows in agg.entity_resolution_queue (resolved_entity_id IS NULL).
    Counterparty / mentioned-entity name lookups awaiting registry match."""

    quarantine_total: int
    """Total rows in agg.filing_event_quarantine. Bodies the extractor
    couldn't process (oversized, parse-failed, low-confidence)."""


# ── DQ M1: heatmap, recency, coverage ──────────────────────────────


class DqHeatmapCellState(StrEnum):
    """Heatmap cell verdict per spec §10.2.

    OK — observation/snapshot within target.
    WARN — within 5 percentage points of target, or recency lag <= 2x SLA.
    CRIT — coverage below (target - 5pp), or recency lag > 2x SLA.
    EMPTY — no recent observation / snapshot for this cell.
    """

    OK = "ok"
    WARN = "warn"
    CRIT = "crit"
    EMPTY = "empty"


class DqHeatmapCellVM(_VMBase):
    """One cell of the /dq/overview 5x4 heatmap.

    `dimension` is one of "recency", "coverage", "validation",
    "spot_check". `state` is the colour bucket; `text` is a short
    glyph (lag in seconds for recency, percentage for coverage, etc.).
    """

    source: Literal["kap", "evds", "bist", "tefas", "mkk"]
    dimension: Literal["recency", "coverage", "validation", "spot_check"]
    state: DqHeatmapCellState
    text: str


class DqOverviewVM(_VMBase):
    """5x4 heatmap (sources x dimensions) for /dq/overview."""

    cells: list[DqHeatmapCellVM]


class DqRecencyRowVM(_VMBase):
    """One bucket of recency aggregate per source.

    `bucket` is "1h", "24h", "7d", or "30d" — the trailing window
    aggregated from `audit.recency_observation`. Aggregates are
    computed server-side via percentile_cont and avg.
    """

    source: Literal["kap", "evds", "bist", "tefas", "mkk"]
    bucket: Literal["1h", "24h", "7d", "30d"]
    avg_lag_seconds: float | None
    p95_lag_seconds: float | None
    max_lag_seconds: int | None
    breach_count: int


class DqRecencyVM(_VMBase):
    """Per-source lag time-series view for /dq/recency."""

    rows: list[DqRecencyRowVM]


class DqCoverageRowVM(_VMBase):
    """Latest coverage snapshot for one (source, dimension)."""

    source: str
    dimension: str
    expected_count: int | None
    actual_count: int | None
    coverage_pct: float | None
    target_pct: float
    observed_at: datetime
    state: DqHeatmapCellState


class DqCoverageVM(_VMBase):
    """Tabular coverage view for /dq/coverage."""

    rows: list[DqCoverageRowVM]


# ── DQ M0 stubs ────────────────────────────────────────────────────


class DqStubVM(_VMBase):
    """M0 placeholder for the seven /dq/* pages.

    M1+ will replace each route with a fully-typed VM (e.g.
    ``DqOverviewVM``, ``DqRecencyVM``, ...) backed by real queries.
    Until then, every stub page renders this VM through the standard
    ``render(request, vm)`` helper so the dashboard's render-only-path
    contract stays uniform across the route table.

    Fields:

      ``title`` — human-readable page title (e.g. "Data Quality —
      Overview"). Rendered into ``<h1>``.

      ``body_message`` — the M0 placeholder body text. Includes the
      milestone where the real surface lands.

      ``stub_id`` — fixed ``"dq-stub"`` literal so the scaffold smoke
      test can grep the response without depending on rendered chrome.
    """

    title: str
    body_message: str
    stub_id: Literal["dq-stub"] = "dq-stub"


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
