"""dq.alert_dispatch — evaluate severity rules + drain pending sinks.

Three public surfaces:

  * ``enqueue(...)``                — INSERT one row into
    ``audit.alert_dispatch`` with status='pending'. The
    ``ad_throttle_dedup`` unique index (rule_name, sink, content_hash,
    minute-bucket of fired_at) makes the call idempotent within the
    1-minute throttle window — re-enqueueing the same condition twice
    inside the same minute is a no-op (returns the existing
    dispatch_id).

  * ``evaluate_and_enqueue(...)``  — for each enabled
    ``audit.severity_rule`` row, run the matching predicate query
    against recent audit.* rows (last ``throttle_seconds``), enqueue
    one ``audit.alert_dispatch`` row per match per sink. Returns the
    count of NEW (non-deduped) rows inserted.

  * ``dispatch_pending(...)``      — drain ``audit.alert_dispatch``
    rows where ``status='pending'``. For each, call the appropriate
    sink (``glitchtip`` / ``email`` / ``slack``). Update
    ``status='delivered'`` (success), ``status='failed'`` (any sink
    error including unknown sink name), or ``status='suppressed'``
    (the sink raised ``SinkNotConfigured`` because env wiring is
    absent). Returns the count successfully delivered.

Tables that don't exist on this branch / environment (e.g.
``audit.bloomberg_comparison_cell`` lands in M4, ``audit.regression_flag``
in M5) are detected via ``information_schema.tables`` and the
corresponding rules are skipped — the dispatcher emits a single
``audit.event(event_type='alert_rule_skipped')`` per sweep so
operators see the gap on /dq.

The dispatcher is designed to be re-run safely. Idempotent properties:

  * The same condition firing twice within ``throttle_seconds`` only
    enqueues once (per the unique index).
  * A pending row that's already been picked up by another worker is
    UPDATE'd by primary key only — no race-bypass row gets written.
  * A failed delivery does NOT auto-retry; ``status='failed'`` is
    terminal. Operators inspect /dq, fix the upstream condition,
    manually mark the row, or wait for the next predicate firing
    (which will land a fresh row at a different fired_at minute).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.db.engine import create_engine
from aslan_core.db.session import create_session_factory
from aslan_core.dq import event as dq_event
from aslan_core.dq.sinks import build_default_sinks
from aslan_core.dq.sinks._base import Sink, SinkNotConfigured
from aslan_core.dq.types import Severity

if TYPE_CHECKING:
    from aslan_core.config import Settings


_log = logging.getLogger(__name__)


# ── Predicate queries ────────────────────────────────────────────


# Each rule's evaluator query: a SELECT that returns the matching
# rows, with whatever payload fields the dispatcher persists onto
# ``audit.alert_dispatch.payload``. The first column MUST be
# ``content_hash_seed`` — a row-stable text identifier the dispatcher
# hashes into the dedup ``content_hash`` (e.g. observation_id,
# snapshot_id). Subsequent columns are folded into the payload dict.
#
# Each query uses the rule's ``throttle_seconds`` as the lookback
# window via ``:throttle_seconds`` bind. Window starts ``now() -
# (throttle_seconds || ' seconds')::interval`` so the dispatcher only
# alerts on freshly-fired conditions, not on old breaches sitting in
# the table.
_RULE_QUERIES: dict[str, str] = {
    "recency_sla_breach": (
        "SELECT "
        "  observation_id::text AS content_hash_seed, "
        "  observation_id, source, lag_seconds, sla_target_seconds, "
        "  observed_at "
        "FROM audit.recency_observation "
        "WHERE sla_breached = true "
        "  AND recorded_at >= now() - make_interval(secs => :throttle_seconds)"
    ),
    "recency_sla_breach_2x": (
        "SELECT "
        "  observation_id::text AS content_hash_seed, "
        "  observation_id, source, lag_seconds, sla_target_seconds, "
        "  observed_at "
        "FROM audit.recency_observation "
        "WHERE lag_seconds > 2 * sla_target_seconds "
        "  AND recorded_at >= now() - make_interval(secs => :throttle_seconds)"
    ),
    "validation_pass_rate_below_95": (
        # Pass-rate per source: complement of failure rate over a
        # rolling 1h window. We approximate "1h pass rate" by counting
        # validation_failure rows; below 95% threshold is mapped to
        # >= the inverse failures count. Approximation: trigger when
        # there are >= 5 failures in the trailing 1h per source.
        # Real rate computation lives at the dashboard layer and the
        # dispatcher uses a coarse threshold.
        "SELECT "
        "  source AS content_hash_seed, "
        "  source, count(*)::int AS failure_count "
        "FROM audit.validation_failure "
        "WHERE detected_at >= now() - INTERVAL '1 hour' "
        "GROUP BY source "
        "HAVING count(*) >= 5 "
        "  AND count(*) < 10"
    ),
    "validation_pass_rate_below_90": (
        "SELECT "
        "  source AS content_hash_seed, "
        "  source, count(*)::int AS failure_count "
        "FROM audit.validation_failure "
        "WHERE detected_at >= now() - INTERVAL '1 hour' "
        "GROUP BY source "
        "HAVING count(*) >= 10"
    ),
    "coverage_below_target": (
        "SELECT "
        "  snapshot_id::text AS content_hash_seed, "
        "  snapshot_id, source, dimension, "
        "  coverage_pct::float AS coverage_pct, "
        "  target_pct::float AS target_pct, observed_at "
        "FROM audit.coverage_snapshot "
        "WHERE coverage_pct IS NOT NULL "
        "  AND coverage_pct < target_pct "
        "  AND coverage_pct >= 90 "
        "  AND recorded_at >= now() - make_interval(secs => :throttle_seconds)"
    ),
    "coverage_below_90pct": (
        "SELECT "
        "  snapshot_id::text AS content_hash_seed, "
        "  snapshot_id, source, dimension, "
        "  coverage_pct::float AS coverage_pct, observed_at "
        "FROM audit.coverage_snapshot "
        "WHERE coverage_pct IS NOT NULL "
        "  AND coverage_pct < 90 "
        "  AND recorded_at >= now() - make_interval(secs => :throttle_seconds)"
    ),
    # Forward-deferred to M4 — table doesn't exist on this branch.
    # The dispatcher's ``_table_present`` guard skips the rule when
    # the underlying table is absent rather than raising.
    "bloomberg_loses_field": (
        "SELECT "
        "  cell_id::text AS content_hash_seed, "
        "  cell_id, source, field, observed_at "
        "FROM audit.bloomberg_comparison_cell "
        "WHERE aslan_advantage = 'loses' "
        "  AND recorded_at >= now() - make_interval(secs => :throttle_seconds)"
    ),
    # Forward-deferred to M5 — table doesn't exist on this branch.
    "regression_flag_critical_entity": (
        "SELECT "
        "  flag_id::text AS content_hash_seed, "
        "  flag_id, entity_id, rule_name AS regression_rule, "
        "  flagged_at "
        "FROM audit.regression_flag "
        "WHERE entity_id = ANY(SELECT entity_id FROM audit.curated_top_50) "
        "  AND recorded_at >= now() - make_interval(secs => :throttle_seconds)"
    ),
    # weekly_scorecard fires from a cron emitting a discrete
    # ``audit.event(event_type='scorecard_generated')`` row. The
    # predicate matches that event-stream signal rather than a typed
    # table.
    "weekly_scorecard": (
        "SELECT "
        "  event_id::text AS content_hash_seed, "
        "  event_id, payload "
        "FROM audit.event "
        "WHERE event_type = 'scorecard_generated' "
        "  AND emitted_at >= now() - make_interval(secs => :throttle_seconds)"
    ),
}


# Map each rule_name to the (schema, table_name) it depends on, so
# the dispatcher can skip rules whose underlying table isn't deployed
# on this branch yet.
_RULE_TABLES: dict[str, tuple[str, str]] = {
    "recency_sla_breach": ("audit", "recency_observation"),
    "recency_sla_breach_2x": ("audit", "recency_observation"),
    "validation_pass_rate_below_95": ("audit", "validation_failure"),
    "validation_pass_rate_below_90": ("audit", "validation_failure"),
    "coverage_below_target": ("audit", "coverage_snapshot"),
    "coverage_below_90pct": ("audit", "coverage_snapshot"),
    "bloomberg_loses_field": ("audit", "bloomberg_comparison_cell"),
    "regression_flag_critical_entity": ("audit", "regression_flag"),
    "weekly_scorecard": ("audit", "event"),
}


# ── enqueue ─────────────────────────────────────────────────────


_INSERT_ALERT_DISPATCH = text(
    "INSERT INTO audit.alert_dispatch("
    "  rule_name, fired_at, sink, status, payload, content_hash"
    ") VALUES ("
    "  :rule_name, :fired_at, :sink, 'pending', "
    "  CAST(:payload AS JSONB), :content_hash"
    ") RETURNING dispatch_id"
)


_SELECT_ALERT_DISPATCH_DEDUP = text(
    "SELECT dispatch_id FROM audit.alert_dispatch "
    "WHERE rule_name = :rule_name "
    "  AND sink = :sink "
    "  AND content_hash = :content_hash "
    "  AND date_trunc('minute', fired_at AT TIME ZONE 'UTC') "
    "      = date_trunc('minute', CAST(:fired_at AS TIMESTAMPTZ) AT TIME ZONE 'UTC') "
    "LIMIT 1"
)


async def enqueue(
    *,
    session: AsyncSession,
    rule_name: str,
    fired_at: datetime,
    sink: str,
    payload: dict[str, Any],
    content_hash: str,
) -> uuid.UUID:
    """INSERT a pending dispatch row. Idempotent within 1-minute window.

    The unique index ``ad_throttle_dedup`` enforces that
    (rule_name, sink, content_hash, minute-bucket(fired_at)) is a
    unique key. A second call with the same arguments inside the same
    minute returns the existing ``dispatch_id`` rather than raising
    or inserting a duplicate.
    """
    payload_json = json.dumps(payload, default=str, sort_keys=True)
    # Wrap the INSERT in a SAVEPOINT so a unique-index violation
    # rolls back only the failed statement, not the surrounding
    # transaction (which may carry other dispatcher work).
    try:
        async with session.begin_nested():
            result = await session.execute(
                _INSERT_ALERT_DISPATCH,
                {
                    "rule_name": rule_name,
                    "fired_at": fired_at,
                    "sink": sink,
                    "payload": payload_json,
                    "content_hash": content_hash,
                },
            )
            new_id = result.scalar_one()
        return uuid.UUID(str(new_id))
    except IntegrityError:
        # Unique-index violation = same condition already enqueued this
        # minute. The SAVEPOINT was rolled back; look up the existing
        # row's dispatch_id so the caller still has a handle.
        existing = await session.execute(
            _SELECT_ALERT_DISPATCH_DEDUP,
            {
                "rule_name": rule_name,
                "sink": sink,
                "content_hash": content_hash,
                "fired_at": fired_at,
            },
        )
        row = existing.one_or_none()
        if row is None:
            # Race we can't reconstruct: re-raise so the caller sees the
            # original integrity error rather than a silent miss.
            raise
        return uuid.UUID(str(row.dispatch_id))


# ── evaluate_and_enqueue ────────────────────────────────────────


_SELECT_ENABLED_RULES = text(
    "SELECT rule_name, severity, sinks, throttle_seconds "
    "FROM audit.severity_rule "
    "WHERE enabled = true "
    "ORDER BY rule_name"
)


async def _table_present(session: AsyncSession, schema: str, name: str) -> bool:
    row = (
        await session.execute(
            text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.tables "
                "  WHERE table_schema = :schema AND table_name = :name"
                ") AS present"
            ),
            {"schema": schema, "name": name},
        )
    ).one()
    return bool(row.present)


def _content_hash(rule_name: str, seed: str) -> str:
    """Stable hash for the dedup index. SHA-256 truncated to 16 hex chars."""
    h = hashlib.sha256(f"{rule_name}:{seed}".encode()).hexdigest()
    return h[:16]


async def evaluate_and_enqueue(
    *,
    session: AsyncSession,
    settings: Settings | None = None,
) -> int:
    """Evaluate all enabled severity rules and enqueue matched alerts.

    For each enabled ``audit.severity_rule`` row whose underlying
    table is deployed on this branch:

      1. Run the matching SELECT in ``_RULE_QUERIES`` over the last
         ``throttle_seconds`` window.
      2. For each matched row, hash the ``content_hash_seed`` value
         into a short stable token.
      3. Enqueue one ``audit.alert_dispatch`` row per (matched-row,
         sink-in-rule.sinks) tuple via ``enqueue()``.

    Returns the number of NEW dispatch rows inserted (deduped re-fires
    within the same minute count as zero).

    Rules whose underlying table isn't deployed are skipped silently
    after emitting one ``audit.event(event_type='alert_rule_skipped')``
    per sweep so the gap is observable.
    """
    _ = settings  # Reserved for future per-rule overrides; unused today.
    rules = (await session.execute(_SELECT_ENABLED_RULES)).all()
    enqueued = 0
    fired_at = datetime.now(UTC)
    for rule in rules:
        rule_name = rule.rule_name
        sinks = list(rule.sinks)
        throttle_seconds = int(rule.throttle_seconds)
        if rule_name not in _RULE_QUERIES:
            # Severity_rule row exists but the dispatcher has no
            # predicate query for it — log + skip.
            await dq_event.emit(
                session=session,
                event_type="alert_rule_skipped",
                emitter="alert-dispatch",
                severity=Severity.WARN,
                payload={
                    "rule_name": rule_name,
                    "reason": "no predicate query registered in alert_dispatch",
                },
            )
            continue
        schema, table = _RULE_TABLES[rule_name]
        if not await _table_present(session, schema, table):
            await dq_event.emit(
                session=session,
                event_type="alert_rule_skipped",
                emitter="alert-dispatch",
                severity=Severity.INFO,
                payload={
                    "rule_name": rule_name,
                    "reason": f"{schema}.{table} not present on this branch",
                },
            )
            continue
        # bloomberg / regression rules also depend on supporting tables
        # (audit.curated_top_50). Wrap the predicate query in a SAVEPOINT
        # so a malformed predicate or missing supporting table doesn't
        # poison the outer transaction (which carries the
        # alert_rule_skipped event emits we still want to commit).
        matches: list[Any]
        try:
            async with session.begin_nested():
                result = await session.execute(
                    text(_RULE_QUERIES[rule_name]),
                    {"throttle_seconds": throttle_seconds},
                )
                matches = list(result.all())
        except Exception as exc:
            _log.warning(
                "alert_dispatch.predicate_failed",
                extra={"rule_name": rule_name, "error": str(exc)},
            )
            await dq_event.emit(
                session=session,
                event_type="alert_rule_skipped",
                emitter="alert-dispatch",
                severity=Severity.WARN,
                payload={
                    "rule_name": rule_name,
                    "reason": "predicate query failed",
                    "error": str(exc),
                },
            )
            continue
        for match in matches:
            row_dict = dict(match._mapping)
            seed = str(row_dict.pop("content_hash_seed"))
            content_hash = _content_hash(rule_name, seed)
            for sink in sinks:
                # ``enqueue`` returns the dispatch_id either way (new
                # insert or pre-existing dedup hit). We can't tell from
                # the UUID whether the row was new, so we INSERT and
                # check the row count via a savepoint-based pre-check:
                # query the dedup index BEFORE inserting. If the row
                # exists already, skip the count increment.
                pre = await session.execute(
                    _SELECT_ALERT_DISPATCH_DEDUP,
                    {
                        "rule_name": rule_name,
                        "sink": sink,
                        "content_hash": content_hash,
                        "fired_at": fired_at,
                    },
                )
                already_present = pre.one_or_none() is not None
                try:
                    await enqueue(
                        session=session,
                        rule_name=rule_name,
                        fired_at=fired_at,
                        sink=sink,
                        payload=row_dict,
                        content_hash=content_hash,
                    )
                except IntegrityError:
                    # Concurrent insert from another worker between
                    # the pre-check and the enqueue. Skip the count.
                    continue
                if not already_present:
                    enqueued += 1
    return enqueued


# ── dispatch_pending ────────────────────────────────────────────


_SELECT_PENDING_DISPATCHES = text(
    "SELECT dispatch_id, rule_name, fired_at, sink, payload, content_hash "
    "FROM audit.alert_dispatch "
    "WHERE status = 'pending' "
    "ORDER BY fired_at ASC "
    "LIMIT :limit"
)


_SELECT_RULE_SEVERITY = text(
    "SELECT severity FROM audit.severity_rule WHERE rule_name = :rule_name"
)


_UPDATE_DELIVERED = text(
    "UPDATE audit.alert_dispatch SET "
    "  status = 'delivered', "
    "  delivered_at = :delivered_at "
    "WHERE dispatch_id = :dispatch_id "
    "  AND status = 'pending'"
)


_UPDATE_FAILED = text(
    "UPDATE audit.alert_dispatch SET "
    "  status = 'failed', "
    "  payload = jsonb_set(payload, '{_dispatch_error}', to_jsonb(CAST(:error AS TEXT))) "
    "WHERE dispatch_id = :dispatch_id "
    "  AND status = 'pending'"
)


_UPDATE_SUPPRESSED = text(
    "UPDATE audit.alert_dispatch SET "
    "  status = 'suppressed', "
    "  payload = jsonb_set(payload, '{_dispatch_suppressed_reason}', "
    "                      to_jsonb(CAST(:reason AS TEXT))) "
    "WHERE dispatch_id = :dispatch_id "
    "  AND status = 'pending'"
)


async def dispatch_pending(
    *,
    settings: Settings,
    sinks: dict[str, Sink] | None = None,
    limit: int = 200,
) -> int:
    """Drain ``audit.alert_dispatch`` rows where ``status='pending'``.

    For each row, fetch the rule's severity from ``audit.severity_rule``
    and call the matching sink. Status transitions:

      * sink returns True              → ``status='delivered'``
      * sink raises ``SinkNotConfigured``→ ``status='suppressed'``
      * sink raises any other Exception → ``status='failed'``
      * unknown sink name              → ``status='failed'``

    The function opens its own engine + session — the dispatcher is
    typically driven by a cron, not a request handler.

    Returns the count of rows transitioned to ``status='delivered'``.
    """
    if sinks is None:
        sinks = build_default_sinks(settings)

    engine = create_engine()
    factory = create_session_factory(engine)
    delivered = 0
    try:
        async with factory() as s:
            pending = (await s.execute(_SELECT_PENDING_DISPATCHES, {"limit": limit})).all()
            await s.commit()
            for row in pending:
                rule_severity = (
                    await s.execute(_SELECT_RULE_SEVERITY, {"rule_name": row.rule_name})
                ).scalar_one_or_none()
                severity = str(rule_severity) if rule_severity is not None else "info"
                payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)

                sink_impl = sinks.get(row.sink)
                if sink_impl is None:
                    await s.execute(
                        _UPDATE_FAILED,
                        {
                            "dispatch_id": row.dispatch_id,
                            "error": f"unknown sink {row.sink!r}; not in dispatcher map",
                        },
                    )
                    await s.commit()
                    continue
                try:
                    ok = await sink_impl.deliver(
                        payload=payload,
                        severity=severity,
                        rule_name=row.rule_name,
                    )
                except SinkNotConfigured as exc:
                    await s.execute(
                        _UPDATE_SUPPRESSED,
                        {"dispatch_id": row.dispatch_id, "reason": str(exc)},
                    )
                    await s.commit()
                    continue
                except Exception as exc:
                    _log.warning(
                        "alert_dispatch.sink_failed",
                        extra={
                            "dispatch_id": str(row.dispatch_id),
                            "sink": row.sink,
                            "rule_name": row.rule_name,
                            "error": str(exc),
                        },
                    )
                    await s.execute(
                        _UPDATE_FAILED,
                        {"dispatch_id": row.dispatch_id, "error": str(exc)[:500]},
                    )
                    await s.commit()
                    continue
                if ok:
                    await s.execute(
                        _UPDATE_DELIVERED,
                        {
                            "dispatch_id": row.dispatch_id,
                            "delivered_at": datetime.now(UTC),
                        },
                    )
                    delivered += 1
                else:
                    await s.execute(
                        _UPDATE_FAILED,
                        {
                            "dispatch_id": row.dispatch_id,
                            "error": "sink returned False",
                        },
                    )
                await s.commit()
    finally:
        await engine.dispose()
    return delivered


__all__ = [
    "dispatch_pending",
    "enqueue",
    "evaluate_and_enqueue",
]
