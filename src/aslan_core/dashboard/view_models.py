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


# ── DQ M2: spot-check ─────────────────────────────────────────────


class DqSpotCheckSampleRowVM(_VMBase):
    """One pending sample on the /dq/spot-check queue.

    `record_pk_summary` is a short text rendering of the JSON PK so
    the queue table can show the labeller "what is this sample about"
    without leaking arbitrary JSON shapes through the type system.
    """

    sample_id: UUID
    source: str
    drawn_at: datetime
    record_table: str
    record_pk_summary: str
    stratum: str | None


class DqSpotCheckQueueVM(_VMBase):
    """Pending-queue view for /dq/spot-check."""

    pending_rows: list[DqSpotCheckSampleRowVM]
    recent_completions: int
    """Count of rows where labelled=true and labelled_at is in the
    trailing 7 days. Surfaces the labelling cadence at a glance."""


class DqSpotCheckResultRowVM(_VMBase):
    """One labelled result row, shown on the per-sample form below
    the per-field input boxes."""

    field: str
    db_value: str | None
    truth_value: str | None
    matches: bool
    variance_pct: float | None
    label_note: str | None
    labeller: str
    recorded_at: datetime


class DqSpotCheckRecordPkPairVM(_VMBase):
    """One (key, value) entry from the sample's record_pk JSON,
    rendered as text. The DB-row pane on /dq/spot-check/<id> shows
    these in order so the labeller can identify the underlying source
    record without the template having to interpret arbitrary JSON."""

    key: str
    value: str


class DqSpotCheckCorroboratorPayloadPairVM(_VMBase):
    """One (key, value) entry from a corroborator-source payload,
    rendered as text on the spot-check page.

    The corroborator panel surfaces these alongside the canonical DB
    row so the labeller can compare a "second opinion" reference value.
    Per NG6: this is reference material — the labeller's truth value
    remains the binding label.
    """

    key: str
    value: str


class DqSpotCheckCorroboratorPanelVM(_VMBase):
    """One corroborator source's panel on the spot-check detail page.

    The panel is read from ``audit.external_corroborator_cache``. When
    no cache row exists for ``(source, entity_ticker)`` the panel
    renders ``cache_state='miss'`` with a Refresh button; on a stale
    row the panel renders ``cache_state='stale'``; on a fresh row the
    panel renders ``cache_state='fresh'``.

    ``implemented=False`` panels render a "not yet implemented"
    placeholder. They appear so the labeller sees the roadmap of
    registered sources without each one needing its own UI work.
    """

    source: str
    """Registered source key (e.g. 'investing_com', 'kap_ir',
    'tradingview', 'earningshub')."""

    entity_ticker: str
    """The BIST ticker the panel keys on. Empty string when no entity
    could be derived from the sample (e.g. a non-KAP sample with no
    ticker projection)."""

    implemented: bool
    """Whether the source has a real fetch handler. Registered-but-
    unimplemented sources render the placeholder."""

    cache_state: Literal["fresh", "stale", "miss", "error", "unimplemented"]
    """`fresh` — within 24-hour TTL.
    `stale` — cache hit but past TTL (still rendered for reference;
    Refresh forces a re-fetch).
    `miss`  — no cache row at all.
    `error` — last cache row carried fetch_status != 'ok'.
    `unimplemented` — source registered but no handler yet."""

    fetched_at: datetime | None
    """Timestamp on the latest cache row, or ``None`` for cache miss /
    unimplemented."""

    cached_age_label: str
    """Human-readable age string (e.g. ``"4 hours ago"``, ``"never"``)."""

    fetch_url: str
    """The URL the corroborator built for this entity, surfaced so the
    labeller can open it in a new tab to cross-check."""

    fetch_latency_ms: int | None
    """Latency of the cached fetch, or ``None`` for cache miss /
    unimplemented."""

    fetch_status: str | None
    """Cached fetch status (``ok``, ``error``, ``rate_limited``,
    ``blocked``), or ``None`` for cache miss / unimplemented."""

    error_summary: str | None
    """First ~200 chars of the error if the cached fetch failed."""

    payload_pairs: list[DqSpotCheckCorroboratorPayloadPairVM]
    """Extracted payload as a deterministic ordered list of pairs."""


class DqSpotCheckSampleDetailVM(_VMBase):
    """Per-sample form view for /dq/spot-check/<sample_id>."""

    sample_id: UUID
    source: str
    drawn_at: datetime
    record_table: str
    record_pk_pairs: list[DqSpotCheckRecordPkPairVM]
    stratum: str | None
    labelled: bool
    labelled_at: datetime | None
    labeller: str | None
    raw_bytes_status: str
    """Human-readable status of the raw-bytes pane:
      * 'kap-replay-pending' — KAP sample, body fetch from MinIO
        deferred to M2.1; v1 renders the doc.filing summary instead.
      * 'api-replay-pending' — non-KAP sample, API replay is M2.1.
    The pane shows this string in v1; live MinIO bytes land in M2.1
    once the per-source filing-id resolution is wired in.
    """
    existing_results: list[DqSpotCheckResultRowVM]
    entity_ticker: str
    """Resolved BIST ticker for the sample, or '' when none could be
    derived. Drives the corroborator panel — empty ticker collapses
    each panel to a "ticker unresolved" message."""

    corroborator_panels: list[DqSpotCheckCorroboratorPanelVM]
    """One panel per registered corroborator source. Empty list when
    ``entity_ticker`` is empty (no fetch is meaningful without a
    ticker)."""


# ── DQ M4: Bloomberg comparison ───────────────────────────────────


class DqBloombergCellRowVM(_VMBase):
    """One cell row for the per-entity grid on /dq/bloomberg."""

    cell_id: UUID
    entity_ticker: str
    field: str
    bloomberg_value: str | None
    aslan_value: str | None
    variance_pct: float | None
    aslan_advantage: Literal["wins", "ties", "loses"] | None


class DqBloombergRunSummaryVM(_VMBase):
    """Per-run summary block for the /dq/bloomberg overview page."""

    run_id: UUID
    quarter: str
    opened_at: datetime
    closed_at: datetime | None
    wins: int
    ties: int
    loses: int
    total_cells: int
    null_bloomberg_cells: int
    """How many cells still need a manual Bloomberg value. The cell-entry
    form surfaces this count so the labeller knows the queue depth."""


class DqBloombergOverviewVM(_VMBase):
    """Top-of-page view: latest run summary + per-cell grid + history."""

    latest_run: DqBloombergRunSummaryVM | None
    """``None`` when no run has been opened yet — the page renders a
    short prompt instead of the grid."""

    cells: list[DqBloombergCellRowVM]
    """All cells of the latest run, ordered by (entity_ticker, field).
    Empty list when ``latest_run`` is None."""

    closed_runs: list[DqBloombergRunSummaryVM]
    """History block — past CLOSED runs, newest first. Click-through
    to ``/dq/bloomberg/runs/<run_id>`` shows that run's grid."""


class DqBloombergRunDetailVM(_VMBase):
    """Per-run detail page for /dq/bloomberg/runs/<run_id>."""

    run: DqBloombergRunSummaryVM
    cells: list[DqBloombergCellRowVM]


# ── DQ M5: validation + regression-flag review ────────────────────


class DqValidationRuleRowVM(_VMBase):
    """One rule's 7-day failure rate on /dq/validation.

    `failures` is the count of ``audit.validation_failure`` rows in
    the trailing 7 days. `severity_breakdown` is a small dict keyed
    by severity → count, frozen by Pydantic on construction.
    """

    rule_name: str
    source: str
    failures_7d: int
    last_failure_at: datetime | None


class DqRegressionFlagRowVM(_VMBase):
    """One open regression flag on /dq/validation.

    Numeric NUMERIC fields are surfaced as Decimal-text via the
    ``Decimal`` typed field; the rendering helper formats them with a
    fixed precision so the page is diff-stable.
    """

    flag_id: int
    source: str
    record_table: str
    record_pk_summary: str
    metric: str
    prior_value: str | None
    current_value: str | None
    shift_pct: str
    threshold_pct: str
    detected_at: datetime
    status: Literal["open", "reviewed", "dismissed", "confirmed_bug"]


class DqXsRuleSkipRowVM(_VMBase):
    """One recent ``xs_rule_skipped`` event from ``audit.event``.

    Surfaces the cron-level gap (a cross-source rule whose dependent
    source tables were absent on the last run) so operators can see
    why no failures landed for a given rule.
    """

    rule_name: str
    reason: str
    missing_summary: str
    emitted_at: datetime


class DqValidationVM(_VMBase):
    """/dq/validation page — 7d rule failure rate + open regression flags
    + recent cross-source rule skips."""

    rule_rows: list[DqValidationRuleRowVM]
    regression_rows: list[DqRegressionFlagRowVM]
    xs_skip_rows: list[DqXsRuleSkipRowVM]


# ── DQ M6: weekly scorecard ───────────────────────────────────────


class DqScorecardRowVM(_VMBase):
    """One metric row on /dq/scorecard.

    All fields are TEXT in the table so they carry units (``"<= 300s"``,
    ``">= 95%"``) verbatim. Status is a closed enum so the renderer
    can colour the cell from a fixed palette.
    """

    metric_name: str
    target: str
    actual: str
    status: Literal["pass", "warn", "fail"]
    notes: str | None


class DqScorecardWeekSummaryVM(_VMBase):
    """One historical week's pass/warn/fail roll-up for the history block."""

    week_start: datetime
    pass_count: int
    warn_count: int
    fail_count: int
    total_count: int
    recorded_at: datetime


class DqScorecardVM(_VMBase):
    """/dq/scorecard — current week + history + email-preview link."""

    current_week_start: datetime
    """The most-recent fully-completed week's Monday (UTC). The dashboard
    shows this week's metrics in the top section."""

    rows: list[DqScorecardRowVM]
    """Rows for ``current_week_start``. Empty list when no scorecard has
    been computed yet — the page renders a short prompt instead of the
    table."""

    pass_pct: float
    pass_count: int
    warn_count: int
    fail_count: int

    history: list[DqScorecardWeekSummaryVM]
    """Past weeks, newest first. Click-through is a future enhancement;
    v1 surfaces the roll-up only."""


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
