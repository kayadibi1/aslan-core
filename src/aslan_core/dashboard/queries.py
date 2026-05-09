"""Static-SQL query helpers for the v0.6.0 dashboard.

Spec §5.1 + §5.2: ``queries.py`` is the ONLY module in
``aslan_core.dashboard`` that may construct SQL via SQLAlchemy
``text(...)``. The alias-aware AST scanner in
``_static_sql_scan.py`` enforces this at lint time. Every other
module imports the helpers below by name.

Each helper is an ``async def`` that takes an ``AsyncSession``,
issues exactly one ``text(...)`` call with parameter placeholders,
and returns either a Pydantic VM or a list of ``dict[str, Any]``
(used for pages where Redis probe data has to be fused server-side
before the final VM is constructed — currently deadletter and
streams).

Forbidden columns from spec §6.3 (``payload``, ``last_error`` raw
text, ``redacted_payload``, ``body_text``, ``metadata``, ``before``,
``after``) NEVER appear in any projection. Column-level GRANT in
migration 0020 is the load-bearing GDPR boundary; any breach here
also fails at the privilege layer with ``InsufficientPrivilegeError``.

Derived projections:

  * ``audit.events.metadata`` → ``metadata_key_count`` via the
    SECURITY DEFINER helper from migration 0020 (and its companion
    GRANTs in migration 0021).
  * ``audit.events.client_ip`` → ``client_ip_truncated`` as a CIDR
    (/24 for IPv4, /48 for IPv6) so per-row IP no longer identifies
    a single subject.
  * ``streams.outbox.last_error`` → ``last_error_kind`` (None for
    now; Task 8 fills in Python-side classification at render time).
  * ``streams.deadletter_log.last_error`` → ``last_error_kind`` (same
    classification path).

Helpers that need Redis-derived data (``deadletter_recent`` →
``redis_state``, ``streams_overview`` → ``xlen``) return dicts /
partial structures; the page handler combines with redis_probes
output and constructs the final frozen VM at render time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from datetime import date as date_cls
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dashboard.view_models import (
    AuditRowVM,
    AuditVM,
    DocumentRowVM,
    DocumentsVM,
    DqBloombergCellRowVM,
    DqBloombergOverviewVM,
    DqBloombergRunDetailVM,
    DqBloombergRunSummaryVM,
    DqCoverageRowVM,
    DqCoverageVM,
    DqHeatmapCellState,
    DqHeatmapCellVM,
    DqOverviewVM,
    DqRecencyRowVM,
    DqRecencyVM,
    DqRegressionFlagRowVM,
    DqScorecardRowVM,
    DqScorecardVM,
    DqScorecardWeekSummaryVM,
    DqSpotCheckQueueVM,
    DqSpotCheckRecordPkPairVM,
    DqSpotCheckResultRowVM,
    DqSpotCheckSampleDetailVM,
    DqSpotCheckSampleRowVM,
    DqValidationRuleRowVM,
    DqValidationVM,
    DqXsRuleSkipRowVM,
    IngestionRowVM,
    IngestionVM,
    OutboxRowVM,
    OutboxVM,
    OverviewVM,
    RedactionRowVM,
    RedactionsVM,
    ReviewVM,
    SeriesRowVM,
    TimeseriesVM,
)
from aslan_core.streams.names import STREAMS

# Sources iterated by the /dq/* heatmap and aggregates. Mirrors
# `aslan_core.dq.probes.SOURCES` but the dashboard module imports
# this list from queries.py so the static-SQL scanner does not also
# need to whitelist `aslan_core.dq.probes`.
_DQ_SOURCES: tuple[str, ...] = ("kap", "evds", "bist", "tefas", "mkk")

# ── Overview ───────────────────────────────────────────────────────


async def overview(session: AsyncSession) -> OverviewVM:
    """Headline counters and ages for the Overview page.

    ``streams_total_xlen`` is left as ``0`` here because it derives
    entirely from Redis — the page handler fills it from
    ``redis_probes`` before constructing the final VM. The other
    fields come from one round-trip with scalar subqueries.
    """
    row = (
        await session.execute(
            text(
                "SELECT "
                "  (SELECT count(*)::int FROM streams.outbox WHERE published_at IS NULL) "
                "    AS outbox_pending, "
                "  COALESCE("
                "    EXTRACT(EPOCH FROM (now() - "
                "      (SELECT min(created_at) FROM streams.outbox WHERE published_at IS NULL)"
                "    ))::float, "
                "    0.0"
                "  ) AS outbox_oldest_age_s, "
                "  (SELECT count(*)::int FROM streams.outbox "
                "     WHERE published_at >= now() - INTERVAL '15 minutes') "
                "    AS outbox_drained_last_15m, "
                "  (SELECT count(*)::int FROM streams.deadletter_log) AS deadletter_total, "
                "  (SELECT count(*)::int FROM streams.deadletter_log "
                "     WHERE routed_at >= now() - INTERVAL '24 hours') AS deadletter_last_24h, "
                "  (SELECT count(*)::int FROM src.ingestion_run "
                "     WHERE started_at >= now() - INTERVAL '24 hours') "
                "    AS ingestion_runs_last_24h, "
                "  COALESCE("
                "    (SELECT count(*)::float FROM audit.events "
                "       WHERE occurred_at >= now() - INTERVAL '60 minutes') / 60.0, "
                "    0.0"
                "  ) AS audit_events_per_min_last_60m, "
                "  (SELECT count(*)::int FROM streams.redaction_registry) "
                "    AS redaction_registry_size, "
                "  (SELECT max(redacted_at) FROM streams.redaction_registry) "
                "    AS redaction_last_at"
            )
        )
    ).one()
    return OverviewVM(
        outbox_pending=row.outbox_pending,
        outbox_oldest_age_s=row.outbox_oldest_age_s,
        outbox_drained_last_15m=row.outbox_drained_last_15m,
        streams_total_xlen=0,
        deadletter_total=row.deadletter_total,
        deadletter_last_24h=row.deadletter_last_24h,
        ingestion_runs_last_24h=row.ingestion_runs_last_24h,
        audit_events_per_min_last_60m=row.audit_events_per_min_last_60m,
        redaction_registry_size=row.redaction_registry_size,
        redaction_last_at=row.redaction_last_at,
    )


# ── Outbox ─────────────────────────────────────────────────────────


async def outbox_recent(session: AsyncSession, *, limit: int = 50, offset: int = 0) -> OutboxVM:
    """Most-recent outbox rows. ``last_error_kind`` is left None here;
    Task 8's page handler classifies on a Python-side mapping (the raw
    ``last_error`` column is forbidden by the column-allowlist GRANT)."""
    rows = (
        await session.execute(
            text(
                "SELECT outbox_id, stream_name, event_id, schema_version, "
                "       source_id, created_at, published_at, publish_attempts, "
                "       last_attempt_at "
                "FROM streams.outbox "
                "ORDER BY created_at DESC "
                "LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset},
        )
    ).all()
    pending = (
        await session.execute(
            text("SELECT count(*)::int FROM streams.outbox WHERE published_at IS NULL")
        )
    ).scalar_one()
    return OutboxVM(
        rows=[
            OutboxRowVM(
                outbox_id=r.outbox_id,
                stream_name=r.stream_name,
                event_id=r.event_id,
                schema_version=r.schema_version,
                source_id=r.source_id,
                created_at=r.created_at,
                published_at=r.published_at,
                publish_attempts=r.publish_attempts,
                last_attempt_at=r.last_attempt_at,
                last_error_kind=None,
            )
            for r in rows
        ],
        pending_total=pending,
    )


# ── Dead-letter ────────────────────────────────────────────────────


async def deadletter_recent(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """Most-recent deadletter rows + total count.

    Returns ``(rows, total)`` where each row is a ``dict`` carrying
    every SQL-derived field. The page handler fuses with Redis probe
    data and constructs the final ``DeadletterVM`` at render time —
    ``redis_state`` is not knowable from SQL alone.

    The LEFT JOIN on ``streams.deadletter_redis_index`` lets the page
    handler distinguish ``MISSING_INDEX`` (durable record exists in
    ``deadletter_log`` but the Redis-side index entry was never
    written — janitor has not yet reconciled, or the XADD failed)
    from rows that are at least durably indexed. ``deadletter_stream``
    is projected so the page handler probes the right Redis stream
    per row."""
    rows = (
        await session.execute(
            text(
                "SELECT dl.failure_id, dl.event_id, dl.stream_name, "
                "       dl.deadletter_stream, dl.group_name, dl.consumer_name, "
                "       dl.failure_count, dl.routed_at, dl.routed_at_redis, "
                "       dl.redis_message_id, "
                "       (dri.failure_id IS NOT NULL) AS has_redis_index "
                "FROM streams.deadletter_log AS dl "
                "LEFT JOIN streams.deadletter_redis_index AS dri "
                "  ON dri.failure_id = dl.failure_id "
                "ORDER BY dl.routed_at DESC "
                "LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset},
        )
    ).all()
    total = (
        await session.execute(text("SELECT count(*)::int FROM streams.deadletter_log"))
    ).scalar_one()
    return (
        [
            {
                "failure_id": r.failure_id,
                "event_id": r.event_id,
                "stream_name": r.stream_name,
                "deadletter_stream": r.deadletter_stream,
                "group_name": r.group_name,
                "consumer_name": r.consumer_name,
                "failure_count": r.failure_count,
                "last_error_kind": None,
                "routed_at": r.routed_at,
                "routed_at_redis": r.routed_at_redis,
                "redis_message_id": r.redis_message_id,
                "has_redis_index": bool(r.has_redis_index),
            }
            for r in rows
        ],
        total,
    )


# ── Streams ────────────────────────────────────────────────────────


def streams_static_list() -> list[str]:
    """Canonical stream-name list for the Streams page. No SQL — the
    page is a Redis-probe-driven view; this helper exists so the page
    module never has to import ``aslan_core.streams.names`` directly
    and so the static-SQL discipline (queries.py is the only place
    that constructs SQL or bridges the streams catalogue) holds."""
    return list(STREAMS)


# ── Ingestion ──────────────────────────────────────────────────────


async def ingestion_recent(session: AsyncSession, *, limit: int = 50) -> IngestionVM:
    rows = (
        await session.execute(
            text(
                "SELECT ingestion_run_id, source_id, job_name, status, "
                "       started_at, finished_at, "
                "       CASE WHEN finished_at IS NOT NULL "
                "            THEN EXTRACT(EPOCH FROM (finished_at - started_at))::float "
                "            ELSE NULL END AS duration_s, "
                "       CASE WHEN rows_written + docs_written > 0 "
                "            THEN (rows_written + docs_written)::int "
                "            ELSE NULL END AS event_count "
                "FROM src.ingestion_run "
                "ORDER BY started_at DESC "
                "LIMIT :limit"
            ),
            {"limit": limit},
        )
    ).all()
    return IngestionVM(
        rows=[
            IngestionRowVM(
                ingestion_run_id=r.ingestion_run_id,
                source_id=r.source_id,
                job_name=r.job_name,
                status=r.status,
                started_at=r.started_at,
                finished_at=r.finished_at,
                duration_s=r.duration_s,
                event_count=r.event_count,
                last_error_kind=None,
            )
            for r in rows
        ],
    )


# ── Documents ──────────────────────────────────────────────────────


async def documents_recent(session: AsyncSession, *, limit: int = 50) -> DocumentsVM:
    rows = (
        await session.execute(
            text(
                "SELECT f.filing_id, f.source_id, f.kind AS filing_kind, "
                "       f.published_at, f.discovered_at AS ingested_at, "
                "       (SELECT count(*)::int FROM doc.filing_attachment a "
                "          WHERE a.filing_id = f.filing_id) AS attachment_count, "
                "       NULL::text AS entity_name, "
                "       NULL::int AS body_size_bytes, "
                "       NULL::timestamptz AS redacted_at "
                "FROM doc.filing f "
                "ORDER BY f.discovered_at DESC "
                "LIMIT :limit"
            ),
            {"limit": limit},
        )
    ).all()
    return DocumentsVM(
        rows=[
            DocumentRowVM(
                filing_id=r.filing_id,
                source_id=r.source_id,
                entity_name=r.entity_name,
                filing_kind=r.filing_kind,
                published_at=r.published_at,
                ingested_at=r.ingested_at,
                attachment_count=r.attachment_count,
                body_size_bytes=r.body_size_bytes,
                redacted_at=r.redacted_at,
            )
            for r in rows
        ],
    )


# ── Timeseries ─────────────────────────────────────────────────────


async def timeseries_overview(session: AsyncSession, *, limit: int = 50) -> TimeseriesVM:
    rows = (
        await session.execute(
            text(
                "SELECT c.series_id, c.series_code, c.source_id, c.metric, "
                "       c.frequency, "
                "       (SELECT max(o.ts) FROM ts.observation o "
                "          WHERE o.series_id = c.series_id) AS last_observation_at, "
                "       (SELECT count(*)::int FROM ts.observation o "
                "          WHERE o.series_id = c.series_id "
                "          AND o.ts >= now() - INTERVAL '24 hours') "
                "         AS observation_count_last_24h, "
                "       false AS has_gap "
                "FROM ts.series_catalog c "
                "ORDER BY c.series_id "
                "LIMIT :limit"
            ),
            {"limit": limit},
        )
    ).all()
    return TimeseriesVM(
        rows=[
            SeriesRowVM(
                series_id=r.series_id,
                series_code=r.series_code,
                source_id=r.source_id,
                metric=r.metric,
                frequency=r.frequency,
                last_observation_at=r.last_observation_at,
                observation_count_last_24h=r.observation_count_last_24h,
                has_gap=r.has_gap,
            )
            for r in rows
        ],
    )


# ── Audit ──────────────────────────────────────────────────────────


async def audit_recent(session: AsyncSession, *, limit: int = 50) -> AuditVM:
    """Recent ``audit.events`` rows.

    Codex branch-state F-1: raw ``client_ip`` and ``user_agent`` are
    no longer in the dashboard role's column-allowlist GRANT (migration
    0022). The truncated CIDR comes from the SECURITY DEFINER helper
    ``audit.event_client_ip_truncated`` (owned by ``aslan_app``, which
    holds SELECT on the underlying column). ``metadata`` is similarly
    behind ``audit.event_metadata_key_count``."""
    rows = (
        await session.execute(
            text(
                "SELECT event_id, occurred_at, actor_id, actor_kind, "
                "       operation, target_schema, target_table, "
                "       audit.event_client_ip_truncated(event_id, occurred_at) "
                "         AS client_ip_truncated, "
                "       audit.event_metadata_key_count(event_id, occurred_at) "
                "         AS metadata_key_count "
                "FROM audit.events "
                "ORDER BY occurred_at DESC "
                "LIMIT :limit"
            ),
            {"limit": limit},
        )
    ).all()
    return AuditVM(
        rows=[
            AuditRowVM(
                event_id=r.event_id,
                occurred_at=r.occurred_at,
                actor_id=r.actor_id,
                actor_kind=r.actor_kind,
                operation=r.operation,
                target_schema=r.target_schema,
                target_table=r.target_table,
                client_ip_truncated=r.client_ip_truncated,
                metadata_key_count=r.metadata_key_count,
            )
            for r in rows
        ],
    )


# ── Redactions ─────────────────────────────────────────────────────


async def redactions_recent(session: AsyncSession, *, limit: int = 50) -> RedactionsVM:
    """Recent redactions. ``redacted_payload`` is forbidden; only the
    hash is projected.

    ``redis_copies_total`` and ``redis_copies_xdeled`` come from
    ``streams.event_id_to_redis`` — that table is the durable index
    of every Redis copy ever created for an event_id; ``redacted_at``
    on that table is set when the Redis copy was XDEL'd. So:

      * ``redis_copies_total`` = COUNT(event_id_to_redis) per event
      * ``redis_copies_xdeled`` = COUNT(event_id_to_redis WHERE redacted_at IS NOT NULL)

    Both are computable from SQL alone — no Redis probe needed."""
    rows = (
        await session.execute(
            text(
                "SELECT rr.event_id, rr.redaction_reason, rr.original_stream, "
                "       rr.redacted_at, rr.redacted_payload_hash, "
                "       rr.original_payload_hash, "
                "       (SELECT count(*)::int FROM streams.event_id_to_redis er "
                "          WHERE er.event_id = rr.event_id) AS redis_copies_total, "
                "       (SELECT count(*)::int FROM streams.event_id_to_redis er "
                "          WHERE er.event_id = rr.event_id "
                "          AND er.redacted_at IS NOT NULL) AS redis_copies_xdeled "
                "FROM streams.redaction_registry AS rr "
                "ORDER BY rr.redacted_at DESC "
                "LIMIT :limit"
            ),
            {"limit": limit},
        )
    ).all()
    return RedactionsVM(
        rows=[
            RedactionRowVM(
                event_id=r.event_id,
                redaction_reason=r.redaction_reason,
                original_stream=r.original_stream,
                redacted_at=r.redacted_at,
                redacted_payload_hash=r.redacted_payload_hash,
                original_payload_hash=r.original_payload_hash,
                redis_copies_total=r.redis_copies_total,
                redis_copies_xdeled=r.redis_copies_xdeled,
            )
            for r in rows
        ],
    )


async def review_overview(session: AsyncSession) -> ReviewVM:
    """Three depth counters across the extractor's review queues.

    Per aslan-event-extractor SCOPE_v2 D26: depth-only at this milestone;
    the per-row resolution UI lands when there's data to act on.
    """
    row = (
        await session.execute(
            text(
                "SELECT "
                "  (SELECT count(*)::int FROM agg.filing_event_review_queue "
                "     WHERE resolved_at IS NULL) AS review_queue_pending, "
                "  (SELECT count(*)::int FROM agg.entity_resolution_queue "
                "     WHERE resolved_entity_id IS NULL) AS entity_resolution_pending, "
                "  (SELECT count(*)::int FROM agg.filing_event_quarantine) "
                "    AS quarantine_total"
            )
        )
    ).one()
    return ReviewVM(
        review_queue_pending=row.review_queue_pending,
        entity_resolution_pending=row.entity_resolution_pending,
        quarantine_total=row.quarantine_total,
    )


# ── DQ M1: heatmap, recency, coverage ─────────────────────────────


def _classify_recency(lag_seconds: int | None, sla: int | None) -> DqHeatmapCellState:
    """Map (lag, sla) to the heatmap colour bucket.

    None lag (no observation) → EMPTY. lag <= sla → OK. lag <= 2 sla
    → WARN. lag > 2 sla → CRIT. None sla treated as missing config.
    """
    if lag_seconds is None or sla is None:
        return DqHeatmapCellState.EMPTY
    if lag_seconds <= sla:
        return DqHeatmapCellState.OK
    if lag_seconds <= 2 * sla:
        return DqHeatmapCellState.WARN
    return DqHeatmapCellState.CRIT


def _classify_coverage(coverage_pct: float | None, target_pct: float) -> DqHeatmapCellState:
    """Coverage cell verdict per spec §10.2.

    None or no snapshot → EMPTY. >= target → OK. >= target - 5pp →
    WARN. otherwise → CRIT.
    """
    if coverage_pct is None:
        return DqHeatmapCellState.EMPTY
    if coverage_pct >= target_pct:
        return DqHeatmapCellState.OK
    if coverage_pct >= target_pct - 5.0:
        return DqHeatmapCellState.WARN
    return DqHeatmapCellState.CRIT


async def _latest_recency_per_source(
    session: AsyncSession,
) -> dict[str, tuple[int, int]]:
    """Return {source: (lag_seconds, sla_target_seconds)} for the
    most-recent recency_observation per source. Sources without an
    observation are absent from the dict."""
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT ON (source) "
                "  source, lag_seconds, sla_target_seconds "
                "FROM audit.recency_observation "
                "ORDER BY source, observed_at DESC"
            )
        )
    ).all()
    return {r.source: (int(r.lag_seconds), int(r.sla_target_seconds)) for r in rows}


async def _latest_coverage_per_source(
    session: AsyncSession,
) -> dict[str, tuple[float | None, float]]:
    """Return {source: (coverage_pct, target_pct)} for the most-recent
    coverage_snapshot per source (any dimension; we pick the freshest)."""
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT ON (source) "
                "  source, coverage_pct, target_pct "
                "FROM audit.coverage_snapshot "
                "ORDER BY source, observed_at DESC"
            )
        )
    ).all()
    return {
        r.source: (
            float(r.coverage_pct) if r.coverage_pct is not None else None,
            float(r.target_pct),
        )
        for r in rows
    }


async def _validation_pass_rate_last_hour(
    session: AsyncSession,
) -> dict[str, float]:
    """Rolling 1-hour validation pass rate per source.

    pass_rate = 1 - (failures / total). For v1 we approximate "total"
    as failures + 100 (the dashboard does not currently observe how
    many records were validated, only how many failed). The denominator
    is conservative; the heatmap cell value should be read as a
    relative trend rather than absolute correctness.

    # TODO(M1.1): wire to a counter of validated records per source so
    the denominator is real.
    """
    rows = (
        await session.execute(
            text(
                "SELECT source, count(*)::int AS failures FROM audit.validation_failure "
                "WHERE detected_at >= now() - INTERVAL '1 hour' "
                "GROUP BY source"
            )
        )
    ).all()
    return {r.source: 1.0 - (int(r.failures) / (int(r.failures) + 100.0)) for r in rows}


async def dq_overview(session: AsyncSession) -> DqOverviewVM:
    """5x4 heatmap of (source, dimension) -> cell.

    Dimensions: recency, coverage, validation, spot_check (M2-deferred).
    Each cell is built from the most-recent observation of that
    (source, dimension)."""
    recency = await _latest_recency_per_source(session)
    coverage_data = await _latest_coverage_per_source(session)
    validation = await _validation_pass_rate_last_hour(session)
    cells: list[DqHeatmapCellVM] = []
    for source in _DQ_SOURCES:
        # Recency cell
        rec = recency.get(source)
        if rec is None:
            cells.append(
                DqHeatmapCellVM(
                    source=source,
                    dimension="recency",
                    state=DqHeatmapCellState.EMPTY,
                    text="—",
                )
            )
        else:
            lag, sla = rec
            cells.append(
                DqHeatmapCellVM(
                    source=source,
                    dimension="recency",
                    state=_classify_recency(lag, sla),
                    text=f"{lag}s / {sla}s",
                )
            )
        # Coverage cell
        cov = coverage_data.get(source)
        if cov is None:
            cells.append(
                DqHeatmapCellVM(
                    source=source,
                    dimension="coverage",
                    state=DqHeatmapCellState.EMPTY,
                    text="—",
                )
            )
        else:
            pct, target = cov
            cells.append(
                DqHeatmapCellVM(
                    source=source,
                    dimension="coverage",
                    state=_classify_coverage(pct, target),
                    text=f"{pct:.1f}%" if pct is not None else "—",
                )
            )
        # Validation cell — pass rate
        val = validation.get(source)
        if val is None:
            cells.append(
                DqHeatmapCellVM(
                    source=source,
                    dimension="validation",
                    state=DqHeatmapCellState.OK,
                    text="100.0%",
                )
            )
        else:
            cells.append(
                DqHeatmapCellVM(
                    source=source,
                    dimension="validation",
                    state=(
                        DqHeatmapCellState.OK
                        if val >= 0.99
                        else DqHeatmapCellState.WARN
                        if val >= 0.95
                        else DqHeatmapCellState.CRIT
                    ),
                    text=f"{val * 100:.1f}%",
                )
            )
        # Spot-check — M2-deferred
        cells.append(
            DqHeatmapCellVM(
                source=source,
                dimension="spot_check",
                state=DqHeatmapCellState.EMPTY,
                text="—",
            )
        )
    return DqOverviewVM(cells=cells)


_RECENCY_BUCKETS: tuple[tuple[str, str], ...] = (
    ("1h", "1 hour"),
    ("24h", "24 hours"),
    ("7d", "7 days"),
    ("30d", "30 days"),
)


async def dq_recency(session: AsyncSession) -> DqRecencyVM:
    """Per-source lag aggregates over four trailing windows.

    Returns one row per (source, bucket) — empty rows omitted. The
    aggregates are computed by Postgres via percentile_cont so the
    page handler does no math.
    """
    rows: list[DqRecencyRowVM] = []
    # We issue four parameterised queries (one per bucket) rather than
    # a UNION ALL to keep the SQL literal in queries.py simple. Each
    # query is on the recency_observation hypertable index
    # (source, observed_at DESC) so all four are sub-millisecond.
    for bucket_label, _interval in _RECENCY_BUCKETS:
        sql = _RECENCY_BUCKET_SQL[bucket_label]
        result_rows = (await session.execute(sql)).all()
        for r in result_rows:
            rows.append(
                DqRecencyRowVM(
                    source=r.source,
                    bucket=bucket_label,
                    avg_lag_seconds=(
                        float(r.avg_lag_seconds) if r.avg_lag_seconds is not None else None
                    ),
                    p95_lag_seconds=(
                        float(r.p95_lag_seconds) if r.p95_lag_seconds is not None else None
                    ),
                    max_lag_seconds=(
                        int(r.max_lag_seconds) if r.max_lag_seconds is not None else None
                    ),
                    breach_count=int(r.breach_count),
                )
            )
    return DqRecencyVM(rows=rows)


# Each bucket gets its own static literal — the static-SQL scanner
# requires text() arguments be `Constant(str)`, which rules out string
# interpolation. This is wordy but lints cleanly.
_RECENCY_BUCKET_SQL: dict[str, Any] = {
    "1h": text(
        "SELECT source, "
        "  avg(lag_seconds)::float AS avg_lag_seconds, "
        "  percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_seconds)::float AS p95_lag_seconds, "
        "  max(lag_seconds)::int AS max_lag_seconds, "
        "  count(*) FILTER (WHERE sla_breached)::int AS breach_count "
        "FROM audit.recency_observation "
        "WHERE observed_at >= now() - INTERVAL '1 hour' "
        "GROUP BY source "
        "ORDER BY source"
    ),
    "24h": text(
        "SELECT source, "
        "  avg(lag_seconds)::float AS avg_lag_seconds, "
        "  percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_seconds)::float AS p95_lag_seconds, "
        "  max(lag_seconds)::int AS max_lag_seconds, "
        "  count(*) FILTER (WHERE sla_breached)::int AS breach_count "
        "FROM audit.recency_observation "
        "WHERE observed_at >= now() - INTERVAL '24 hours' "
        "GROUP BY source "
        "ORDER BY source"
    ),
    "7d": text(
        "SELECT source, "
        "  avg(lag_seconds)::float AS avg_lag_seconds, "
        "  percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_seconds)::float AS p95_lag_seconds, "
        "  max(lag_seconds)::int AS max_lag_seconds, "
        "  count(*) FILTER (WHERE sla_breached)::int AS breach_count "
        "FROM audit.recency_observation "
        "WHERE observed_at >= now() - INTERVAL '7 days' "
        "GROUP BY source "
        "ORDER BY source"
    ),
    "30d": text(
        "SELECT source, "
        "  avg(lag_seconds)::float AS avg_lag_seconds, "
        "  percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_seconds)::float AS p95_lag_seconds, "
        "  max(lag_seconds)::int AS max_lag_seconds, "
        "  count(*) FILTER (WHERE sla_breached)::int AS breach_count "
        "FROM audit.recency_observation "
        "WHERE observed_at >= now() - INTERVAL '30 days' "
        "GROUP BY source "
        "ORDER BY source"
    ),
}


async def dq_coverage(session: AsyncSession) -> DqCoverageVM:
    """Latest coverage_snapshot per (source, dimension)."""
    result = (
        await session.execute(
            text(
                "SELECT DISTINCT ON (source, dimension) "
                "  source, dimension, expected_count, actual_count, "
                "  coverage_pct, target_pct, observed_at "
                "FROM audit.coverage_snapshot "
                "ORDER BY source, dimension, observed_at DESC"
            )
        )
    ).all()
    rows: list[DqCoverageRowVM] = []
    for r in result:
        pct = float(r.coverage_pct) if r.coverage_pct is not None else None
        target = float(r.target_pct)
        rows.append(
            DqCoverageRowVM(
                source=r.source,
                dimension=r.dimension,
                expected_count=int(r.expected_count) if r.expected_count is not None else None,
                actual_count=int(r.actual_count) if r.actual_count is not None else None,
                coverage_pct=pct,
                target_pct=target,
                observed_at=r.observed_at,
                state=_classify_coverage(pct, target),
            )
        )
    return DqCoverageVM(rows=rows)


# ── DQ M2: spot-check ─────────────────────────────────────────────


def _summarise_record_pk(record_pk: Any) -> str:
    """Render a JSON record_pk as a short ``k=v, k=v`` string.

    The dashboard never round-trips arbitrary JSON through the template
    — the per-key `record_pk_pairs` carries the structured shape; this
    summary is for the queue table only."""
    if record_pk is None:
        return ""
    if isinstance(record_pk, str):
        try:
            record_pk = json.loads(record_pk)
        except (ValueError, TypeError):
            return str(record_pk)
    if not isinstance(record_pk, dict):
        return str(record_pk)
    return ", ".join(f"{k}={v}" for k, v in sorted(record_pk.items()))


def _record_pk_pairs(record_pk: Any) -> list[DqSpotCheckRecordPkPairVM]:
    if isinstance(record_pk, str):
        try:
            record_pk = json.loads(record_pk)
        except (ValueError, TypeError):
            return []
    if not isinstance(record_pk, dict):
        return []
    return [
        DqSpotCheckRecordPkPairVM(key=str(k), value=str(v)) for k, v in sorted(record_pk.items())
    ]


async def dq_spot_check_queue(session: AsyncSession, *, limit: int = 50) -> DqSpotCheckQueueVM:
    """Pending samples + 7-day completion count for /dq/spot-check."""
    pending = (
        await session.execute(
            text(
                "SELECT sample_id, source, drawn_at, record_table, "
                "  record_pk, stratum "
                "FROM audit.spot_check_sample "
                "WHERE labelled = false "
                "ORDER BY drawn_at DESC "
                "LIMIT :limit"
            ),
            {"limit": limit},
        )
    ).all()
    completions = (
        await session.execute(
            text(
                "SELECT count(*)::int AS n FROM audit.spot_check_sample "
                "WHERE labelled = true "
                "  AND labelled_at >= now() - INTERVAL '7 days'"
            )
        )
    ).scalar_one()
    rows: list[DqSpotCheckSampleRowVM] = []
    for r in pending:
        rows.append(
            DqSpotCheckSampleRowVM(
                sample_id=r.sample_id,
                source=r.source,
                drawn_at=r.drawn_at,
                record_table=r.record_table,
                record_pk_summary=_summarise_record_pk(r.record_pk),
                stratum=r.stratum,
            )
        )
    return DqSpotCheckQueueVM(
        pending_rows=rows,
        recent_completions=int(completions),
    )


async def dq_spot_check_sample_detail(
    session: AsyncSession, *, sample_id: UUID
) -> DqSpotCheckSampleDetailVM | None:
    """Sample detail + existing label results for /dq/spot-check/<id>."""
    sample_row = (
        await session.execute(
            text(
                "SELECT sample_id, source, drawn_at, record_table, "
                "  record_pk, stratum, labelled, labelled_at, labeller "
                "FROM audit.spot_check_sample "
                "WHERE sample_id = :sid"
            ),
            {"sid": sample_id},
        )
    ).one_or_none()
    if sample_row is None:
        return None

    result_rows = (
        await session.execute(
            text(
                "SELECT field, db_value, truth_value, matches, "
                "  variance_pct, label_note, labeller, recorded_at "
                "FROM audit.spot_check_result "
                "WHERE sample_id = :sid "
                "ORDER BY recorded_at"
            ),
            {"sid": sample_id},
        )
    ).all()
    results: list[DqSpotCheckResultRowVM] = [
        DqSpotCheckResultRowVM(
            field=r.field,
            db_value=r.db_value,
            truth_value=r.truth_value,
            matches=bool(r.matches),
            variance_pct=float(r.variance_pct) if r.variance_pct is not None else None,
            label_note=r.label_note,
            labeller=r.labeller,
            recorded_at=r.recorded_at,
        )
        for r in result_rows
    ]
    raw_bytes_status = "kap-replay-pending" if sample_row.source == "kap" else "api-replay-pending"
    return DqSpotCheckSampleDetailVM(
        sample_id=sample_row.sample_id,
        source=sample_row.source,
        drawn_at=sample_row.drawn_at,
        record_table=sample_row.record_table,
        record_pk_pairs=_record_pk_pairs(sample_row.record_pk),
        stratum=sample_row.stratum,
        labelled=bool(sample_row.labelled),
        labelled_at=sample_row.labelled_at,
        labeller=sample_row.labeller,
        raw_bytes_status=raw_bytes_status,
        existing_results=results,
    )


# ── DQ M4: Bloomberg-comparison ───────────────────────────────────


_BLOOMBERG_LATEST_RUN_SQL = text(
    "SELECT run_id, quarter, opened_at, closed_at "
    "FROM audit.bloomberg_comparison_run "
    "ORDER BY opened_at DESC LIMIT 1"
)


_BLOOMBERG_RUN_BY_ID_SQL = text(
    "SELECT run_id, quarter, opened_at, closed_at "
    "FROM audit.bloomberg_comparison_run WHERE run_id = :run_id"
)


_BLOOMBERG_CELLS_FOR_RUN_SQL = text(
    "SELECT cell_id, entity_ticker, field, bloomberg_value, aslan_value, "
    "  variance_pct, aslan_advantage "
    "FROM audit.bloomberg_comparison_cell "
    "WHERE run_id = :run_id "
    "ORDER BY entity_ticker, field"
)


_BLOOMBERG_CLOSED_RUNS_SQL = text(
    "SELECT run_id, quarter, opened_at, closed_at "
    "FROM audit.bloomberg_comparison_run "
    "WHERE closed_at IS NOT NULL "
    "ORDER BY closed_at DESC LIMIT :limit"
)


_BLOOMBERG_RUN_AGGREGATES_SQL = text(
    "SELECT "
    "  count(*) FILTER (WHERE aslan_advantage = 'wins')::int AS wins, "
    "  count(*) FILTER (WHERE aslan_advantage = 'ties')::int AS ties, "
    "  count(*) FILTER (WHERE aslan_advantage = 'loses')::int AS loses, "
    "  count(*)::int AS total_cells, "
    "  count(*) FILTER (WHERE bloomberg_value IS NULL)::int AS null_bloomberg "
    "FROM audit.bloomberg_comparison_cell WHERE run_id = :run_id"
)


async def _bloomberg_run_summary(session: AsyncSession, *, run_row: Any) -> DqBloombergRunSummaryVM:
    aggs = (await session.execute(_BLOOMBERG_RUN_AGGREGATES_SQL, {"run_id": run_row.run_id})).one()
    return DqBloombergRunSummaryVM(
        run_id=run_row.run_id,
        quarter=str(run_row.quarter),
        opened_at=run_row.opened_at,
        closed_at=run_row.closed_at,
        wins=int(aggs.wins),
        ties=int(aggs.ties),
        loses=int(aggs.loses),
        total_cells=int(aggs.total_cells),
        null_bloomberg_cells=int(aggs.null_bloomberg),
    )


def _bloomberg_cell_row(row: Any) -> DqBloombergCellRowVM:
    advantage = row.aslan_advantage if row.aslan_advantage in {"wins", "ties", "loses"} else None
    return DqBloombergCellRowVM(
        cell_id=row.cell_id,
        entity_ticker=str(row.entity_ticker),
        field=str(row.field),
        bloomberg_value=row.bloomberg_value,
        aslan_value=row.aslan_value,
        variance_pct=float(row.variance_pct) if row.variance_pct is not None else None,
        aslan_advantage=advantage,
    )


async def dq_bloomberg_overview(session: AsyncSession) -> DqBloombergOverviewVM:
    """Latest run summary + cells + history block for /dq/bloomberg."""
    latest = (await session.execute(_BLOOMBERG_LATEST_RUN_SQL)).one_or_none()
    if latest is None:
        return DqBloombergOverviewVM(
            latest_run=None,
            cells=[],
            closed_runs=[],
        )
    summary = await _bloomberg_run_summary(session, run_row=latest)
    cell_rows = (
        await session.execute(_BLOOMBERG_CELLS_FOR_RUN_SQL, {"run_id": latest.run_id})
    ).all()
    cells = [_bloomberg_cell_row(r) for r in cell_rows]
    closed_run_rows = (await session.execute(_BLOOMBERG_CLOSED_RUNS_SQL, {"limit": 12})).all()
    closed_runs: list[DqBloombergRunSummaryVM] = []
    for cr in closed_run_rows:
        if cr.run_id == latest.run_id:
            # The latest_run block already carries the latest run's
            # aggregates; skip it here so the History list is strictly
            # "older than latest".
            continue
        closed_runs.append(await _bloomberg_run_summary(session, run_row=cr))
    return DqBloombergOverviewVM(
        latest_run=summary,
        cells=cells,
        closed_runs=closed_runs,
    )


async def dq_bloomberg_run_detail(
    session: AsyncSession, *, run_id: UUID
) -> DqBloombergRunDetailVM | None:
    """One-run grid view for /dq/bloomberg/runs/<run_id>."""
    run_row = (await session.execute(_BLOOMBERG_RUN_BY_ID_SQL, {"run_id": run_id})).one_or_none()
    if run_row is None:
        return None
    summary = await _bloomberg_run_summary(session, run_row=run_row)
    cell_rows = (
        await session.execute(_BLOOMBERG_CELLS_FOR_RUN_SQL, {"run_id": run_row.run_id})
    ).all()
    cells = [_bloomberg_cell_row(r) for r in cell_rows]
    return DqBloombergRunDetailVM(run=summary, cells=cells)


# ── DQ M5: validation page ────────────────────────────────────────


_VALIDATION_RULE_FAILURES_7D_SQL = text(
    "SELECT rule_name, source, "
    "  count(*)::int AS failures_7d, "
    "  max(detected_at) AS last_failure_at "
    "FROM audit.validation_failure "
    "WHERE detected_at >= now() - INTERVAL '7 days' "
    "GROUP BY rule_name, source "
    "ORDER BY failures_7d DESC, rule_name "
    "LIMIT :limit"
)


_VALIDATION_OPEN_FLAGS_SQL = text(
    "SELECT flag_id, source, record_table, record_pk, metric, "
    "  prior_value, current_value, shift_pct, threshold_pct, "
    "  detected_at, status "
    "FROM audit.regression_flag "
    "WHERE status = 'open' "
    "ORDER BY detected_at DESC "
    "LIMIT :limit"
)


_VALIDATION_XS_SKIPS_SQL = text(
    "SELECT payload, emitted_at "
    "FROM audit.event "
    "WHERE event_type = 'xs_rule_skipped' "
    "  AND emitted_at >= now() - INTERVAL '7 days' "
    "ORDER BY emitted_at DESC "
    "LIMIT :limit"
)


def _format_decimal(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


async def dq_validation(
    session: AsyncSession,
    *,
    rule_limit: int = 50,
    flag_limit: int = 25,
    skip_limit: int = 25,
) -> DqValidationVM:
    """/dq/validation — 7d rule failure counts + open regression flags +
    recent cross-source rule skips.

    Spec §10.7. The page is read-only; the regression-flag review POST
    handler in pages/dq_validation.py mutates ``audit.regression_flag``
    via ``dq.regression.set_status`` and 303-redirects back here.
    """
    rule_rows_raw = (
        await session.execute(_VALIDATION_RULE_FAILURES_7D_SQL, {"limit": rule_limit})
    ).all()
    rule_rows: list[DqValidationRuleRowVM] = [
        DqValidationRuleRowVM(
            rule_name=r.rule_name,
            source=r.source,
            failures_7d=int(r.failures_7d),
            last_failure_at=r.last_failure_at,
        )
        for r in rule_rows_raw
    ]

    flag_rows_raw = (await session.execute(_VALIDATION_OPEN_FLAGS_SQL, {"limit": flag_limit})).all()
    regression_rows: list[DqRegressionFlagRowVM] = [
        DqRegressionFlagRowVM(
            flag_id=int(r.flag_id),
            source=r.source,
            record_table=r.record_table,
            record_pk_summary=_summarise_record_pk(r.record_pk),
            metric=r.metric,
            prior_value=_format_decimal(r.prior_value),
            current_value=_format_decimal(r.current_value),
            shift_pct=str(r.shift_pct),
            threshold_pct=str(r.threshold_pct),
            detected_at=r.detected_at,
            status=r.status,
        )
        for r in flag_rows_raw
    ]

    skip_rows_raw = (await session.execute(_VALIDATION_XS_SKIPS_SQL, {"limit": skip_limit})).all()
    xs_skip_rows: list[DqXsRuleSkipRowVM] = []
    for r in skip_rows_raw:
        payload = r.payload
        if isinstance(payload, str):
            payload_dict: dict[str, Any] = json.loads(payload)
        elif isinstance(payload, dict):
            payload_dict = payload
        else:
            payload_dict = {}
        rule_name = str(payload_dict.get("rule_name") or "(unknown)")
        reason = str(payload_dict.get("reason") or "(unspecified)")
        missing_value = payload_dict.get("missing")
        if isinstance(missing_value, list):
            missing_summary = ", ".join(str(x) for x in missing_value) or "—"
        else:
            missing_summary = "—"
        xs_skip_rows.append(
            DqXsRuleSkipRowVM(
                rule_name=rule_name,
                reason=reason,
                missing_summary=missing_summary,
                emitted_at=r.emitted_at,
            )
        )

    return DqValidationVM(
        rule_rows=rule_rows,
        regression_rows=regression_rows,
        xs_skip_rows=xs_skip_rows,
    )


# ── DQ M6: weekly scorecard ───────────────────────────────────────


_SCORECARD_LATEST_WEEK_SQL = text(
    "SELECT max(week_start) AS week_start FROM audit.scorecard_snapshot"
)


_SCORECARD_ROWS_BY_WEEK_SQL = text(
    "SELECT week_start, metric_name, target, actual, status, notes "
    "FROM audit.scorecard_snapshot "
    "WHERE week_start = :week_start "
    "ORDER BY metric_name"
)


_SCORECARD_HISTORY_SQL = text(
    "SELECT week_start, "
    "  count(*) FILTER (WHERE status = 'pass')::int AS pass_count, "
    "  count(*) FILTER (WHERE status = 'warn')::int AS warn_count, "
    "  count(*) FILTER (WHERE status = 'fail')::int AS fail_count, "
    "  count(*)::int AS total_count, "
    "  max(recorded_at) AS recorded_at "
    "FROM audit.scorecard_snapshot "
    "GROUP BY week_start "
    "ORDER BY week_start DESC "
    "LIMIT :limit"
)


_SCORECARD_LATEST_EVENT_SQL = text(
    "SELECT payload, emitted_at FROM audit.event "
    "WHERE event_type = 'scorecard_generated' "
    "ORDER BY emitted_at DESC LIMIT 1"
)


def _date_to_dt(value: Any) -> datetime:
    """Convert a ``DATE`` (or already-``datetime``) to an aware UTC ``datetime``.

    The dashboard VM uses ``datetime`` (per the type-safety allowlist),
    so we promote DATE columns at the query boundary.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, date_cls):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    raise TypeError(f"unexpected week_start type {type(value)!r}")


async def dq_scorecard(
    session: AsyncSession,
    *,
    history_limit: int = 12,
) -> DqScorecardVM:
    """/dq/scorecard — current-week metrics + history roll-up.

    Spec §10.8. The page is read-only; the cron writes new weeks. When
    no scorecard rows exist (fresh DB / first deploy), the VM carries
    an empty ``rows`` list and the renderer shows a short prompt.
    """
    latest_row = (await session.execute(_SCORECARD_LATEST_WEEK_SQL)).one_or_none()
    if latest_row is None or latest_row.week_start is None:
        # No scorecard yet — placeholder. Dashboard renders a short
        # "run aslan-core audit scorecard to populate" prompt.
        placeholder = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return DqScorecardVM(
            current_week_start=placeholder,
            rows=[],
            pass_pct=0.0,
            pass_count=0,
            warn_count=0,
            fail_count=0,
            history=[],
        )
    week_start_value = latest_row.week_start
    rows_raw = (
        await session.execute(
            _SCORECARD_ROWS_BY_WEEK_SQL,
            {"week_start": week_start_value},
        )
    ).all()
    rows: list[DqScorecardRowVM] = [
        DqScorecardRowVM(
            metric_name=r.metric_name,
            target=r.target,
            actual=r.actual,
            status=r.status,
            notes=r.notes,
        )
        for r in rows_raw
    ]
    pass_count = sum(1 for r in rows if r.status == "pass")
    warn_count = sum(1 for r in rows if r.status == "warn")
    fail_count = sum(1 for r in rows if r.status == "fail")
    pass_pct = (100.0 * pass_count / len(rows)) if rows else 0.0

    history_raw = (
        await session.execute(_SCORECARD_HISTORY_SQL, {"limit": history_limit})
    ).all()
    history: list[DqScorecardWeekSummaryVM] = [
        DqScorecardWeekSummaryVM(
            week_start=_date_to_dt(h.week_start),
            pass_count=int(h.pass_count),
            warn_count=int(h.warn_count),
            fail_count=int(h.fail_count),
            total_count=int(h.total_count),
            recorded_at=h.recorded_at,
        )
        for h in history_raw
    ]
    return DqScorecardVM(
        current_week_start=_date_to_dt(week_start_value),
        rows=rows,
        pass_pct=pass_pct,
        pass_count=pass_count,
        warn_count=warn_count,
        fail_count=fail_count,
        history=history,
    )


async def dq_scorecard_latest_email_body(session: AsyncSession) -> str | None:
    """Latest ``audit.event(event_type='scorecard_generated')`` body_html.

    Used by the /dq/scorecard ``Email preview`` button so the page can
    render the exact HTML body the cron handed off to the email sink.
    Returns None when no scorecard event has been emitted yet.
    """
    row = (await session.execute(_SCORECARD_LATEST_EVENT_SQL)).one_or_none()
    if row is None or row.payload is None:
        return None
    payload = row.payload
    if isinstance(payload, str):
        payload_dict: dict[str, Any] = json.loads(payload)
    elif isinstance(payload, dict):
        payload_dict = payload
    else:
        return None
    body = payload_dict.get("body_html")
    if isinstance(body, str):
        return body
    return None


__all__ = [
    "audit_recent",
    "deadletter_recent",
    "documents_recent",
    "dq_bloomberg_overview",
    "dq_bloomberg_run_detail",
    "dq_coverage",
    "dq_overview",
    "dq_recency",
    "dq_scorecard",
    "dq_scorecard_latest_email_body",
    "dq_spot_check_queue",
    "dq_spot_check_sample_detail",
    "dq_validation",
    "ingestion_recent",
    "outbox_recent",
    "overview",
    "redactions_recent",
    "review_overview",
    "streams_static_list",
    "timeseries_overview",
]
