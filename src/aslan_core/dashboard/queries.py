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

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dashboard.view_models import (
    AuditRowVM,
    AuditVM,
    DocumentRowVM,
    DocumentsVM,
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


__all__ = [
    "audit_recent",
    "deadletter_recent",
    "documents_recent",
    "ingestion_recent",
    "outbox_recent",
    "overview",
    "redactions_recent",
    "review_overview",
    "streams_static_list",
    "timeseries_overview",
]
