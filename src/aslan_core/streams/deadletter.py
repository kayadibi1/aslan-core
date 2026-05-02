"""Shared dead-letter routing procedure.

Codex spec §7 + F2/F5/F9/F14/F16/F17/F18/F19/F20/F21/F22.

This module is the source of truth for the durable Postgres-first
6-step routing protocol. The :class:`StreamConsumer` (Task 14) and the
janitor (Task 17) both call into :func:`route_to_deadletter`; the
:func:`acquire_or_adopt_intent` procedure is reused unchanged on both
sides so the orphan-finding logic, the FOR-UPDATE row lock, and the
audit emission are defined ONCE rather than duplicated.

Public surface:

* :func:`route_to_deadletter` — the full 6-step routing protocol.
  Returns one of :class:`RoutingComplete`,
  :class:`RoutingReconciledByAdoption`, or :class:`RoutingAborted`
  reflecting the post-routing PEL state the caller must enforce.
* :func:`acquire_or_adopt_intent` — multi-step intent acquisition /
  adoption (codex F21 round 10). Three branches: fresh-insert,
  same-owner retry, stale-adoption (which itself splits into
  reconciled-by-us, replace-and-own, or abort-due-to-fresh-other-owner).
  F22 round 11+12: ``_attempt: int = 0`` keyword-only parameter caps
  re-entry depth at 3.
* :func:`find_orphan_in_deadletter_stream` — paginated Redis-ID-bounded
  XRANGE walk (codex F19 round 8). Captures an upper bound at scan
  start; pages of ``COUNT 10000`` from ``lower_bound`` through the
  captured upper bound; classification "lost" only after the FULL
  pagination completes.
* :func:`redis_id_increment` / :func:`redis_id_compare` — helpers for
  walking Redis stream IDs in lexicographic order.

The protocol assumes the caller holds an in-flight in-process Redis
client and a Postgres :class:`async_sessionmaker`. Each step opens its
own short-lived AsyncSession so a long-running routing call doesn't
hold a Postgres connection across Redis I/O.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from redis.exceptions import RedisError
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.audit import AuditRecord
from aslan_core.audit import record as audit_record
from aslan_core.errors import StreamRoutingContention
from aslan_core.observability.metrics import (
    _KNOWN_CONSUMER_GROUPS,
    _KNOWN_STREAMS,
    _normalize_metric_label,
    aslan_stream_deadletter_index_recovered_total,
    aslan_stream_deadletter_intent_adopted_total,
    aslan_stream_deadletter_orphans_lost_total,
    aslan_stream_deadletter_total,
    aslan_stream_deadletter_xadd_rollback_total,
)
from aslan_core.streams.names import normalize_bist_ticks_label

if TYPE_CHECKING:  # pragma: no cover — typing-only import
    from redis.asyncio import Redis


_log = logging.getLogger(__name__)


def _aw(x: Any) -> Awaitable[Any]:
    """Cast a redis-py return value to ``Awaitable[Any]`` (see
    consumer._aw)."""
    return cast(Awaitable[Any], x)


# ── Public results returned by ``route_to_deadletter`` ──────────────


@dataclass(frozen=True, slots=True)
class RoutingComplete:
    """Routing reached step 5 successfully (this attempt did the XADD
    and the log-row UPDATE). Caller XACKs the original message."""

    redis_message_id: str


@dataclass(frozen=True, slots=True)
class RoutingReconciledByAdoption:
    """``acquire_or_adopt_intent`` returned ``"reconciled_by_us"`` —
    routing for ``failure_id`` was already complete in Redis (the prior
    owner's XADD landed and we discovered it during stale adoption).
    Caller XACKs the original message AND skips the step-6
    ``stream.deadletter`` audit (the
    ``stream.deadletter_orphan_reconciled`` audit emitted inside the
    procedure covers it)."""

    redis_message_id: str


@dataclass(frozen=True, slots=True)
class RoutingAborted:
    """``acquire_or_adopt_intent`` returned ``"abort"`` — a different
    worker holds a fresh intent. Caller does NOT XACK; the message
    stays in PEL and Redis re-delivers."""


RoutingResult = RoutingComplete | RoutingReconciledByAdoption | RoutingAborted


# ── ``acquire_or_adopt_intent`` result ─────────────────────────────


@dataclass(frozen=True, slots=True)
class AdoptionResult:
    """Codex F21 round 10 — the four outcomes of
    :func:`acquire_or_adopt_intent`."""

    action: Literal["own", "reconciled_by_us", "abort"]
    lower_bound: str | None
    redis_message_id: str | None = None


# ── Redis ID helpers ───────────────────────────────────────────────


def redis_id_increment(rid: str) -> str:
    """Bump ``<unix_ms>-<seq>`` to the next valid ID.

    If ``seq`` is at ``2^64 - 1``, wrap to ``<unix_ms + 1>-0``.
    """
    ms_str, seq_str = rid.split("-")
    ms = int(ms_str)
    seq = int(seq_str)
    if seq < (1 << 64) - 1:
        return f"{ms}-{seq + 1}"
    return f"{ms + 1}-0"


def redis_id_compare(a: str, b: str) -> int:
    """Numeric compare on ``<unix_ms>-<seq>`` halves.

    Returns -1, 0, or 1.
    """
    a_ms, a_seq = a.split("-")
    b_ms, b_seq = b.split("-")
    if int(a_ms) != int(b_ms):
        return -1 if int(a_ms) < int(b_ms) else 1
    if int(a_seq) != int(b_seq):
        return -1 if int(a_seq) < int(b_seq) else 1
    return 0


# ── F19 round 8 — paginated Redis-ID-bounded XRANGE ────────────────


async def find_orphan_in_deadletter_stream(
    redis: Redis,
    stream: str,
    *,
    lower_bound: str,
    target_failure_id: int,
    page_size: int = 10_000,
) -> str | None:
    """Walk ``<stream>.deadletter`` from ``lower_bound`` through a
    captured upper bound, looking for an entry whose ``failure_id``
    field equals ``target_failure_id``.

    Pagination required because XRANGE returns at most ``COUNT``
    entries per call. Capture the upper bound ONCE at scan start (via
    ``XINFO STREAM`` last-entry, ``"0-0"`` if empty); concurrent
    XADDs after the capture are NOT included so the work is bounded
    and termination is guaranteed even on a hot stream.

    :returns: The orphan's Redis entry ID, or ``None`` if not found
        across the FULL pagination.
    """
    deadletter_stream = f"{stream}.deadletter"
    # F5 (codex post-merge): propagate transient Redis errors. The
    # original ``except RedisError: return None`` masked connection
    # failures as "orphan absent", causing callers to redrive a
    # duplicate or audit ``stream.deadletter_orphan_lost``. We still
    # treat the "stream key does not exist" case as a legitimate empty
    # state so the new pass-2 reconciliation path doesn't fail when
    # the dead-letter stream has yet to receive its first XADD.
    try:
        info = await redis.xinfo_stream(deadletter_stream)
    except RedisError as exc:
        if "no such key" in str(exc).lower():
            return None
        raise
    last_entry = info.get("last-entry") if info else None
    if not last_entry:
        return None  # empty stream
    upper_bound = _decode(last_entry[0])
    if upper_bound == "0-0":
        return None

    cursor = lower_bound
    while True:
        page = await redis.xrange(
            deadletter_stream,
            min=cursor,
            max=upper_bound,
            count=page_size,
        )
        if not page:
            return None
        for entry_id, fields in page:
            decoded_fields = {
                (k.decode() if isinstance(k, bytes) else k): (
                    v.decode() if isinstance(v, bytes) else v
                )
                for k, v in fields.items()
            }
            try:
                fid_value = int(decoded_fields.get("failure_id", "-1"))
            except (TypeError, ValueError):
                continue
            if fid_value == target_failure_id:
                return _decode(entry_id)
        last_id = _decode(page[-1][0])
        cursor = redis_id_increment(last_id)
        if redis_id_compare(cursor, upper_bound) > 0:
            return None


# ── F21 round 10 — acquire_or_adopt_intent ─────────────────────────


async def acquire_or_adopt_intent(
    session: AsyncSession,
    redis: Redis,
    *,
    fid: int,
    my_owner_id: str,
    my_lower_bound: str,
    stream: str,
    _attempt: int = 0,
) -> AdoptionResult:
    """Multi-step intent acquisition / adoption (codex F21 round 10).

    Three branches:

    1. **Fresh insert** — common path; no prior intent row.
    2. **Same-owner retry** — refresh heartbeat, preserve existing
       ``redis_lower_bound_id`` (covers any XADD this worker already
       issued).
    3. **Stale adoption** — different worker, heartbeat older than 60s.
       Sub-outcomes:
         - **Reconciled-by-us**: prior owner's XADD landed; index it,
           UPDATE the log row, DELETE the intent, emit
           ``stream.deadletter_orphan_reconciled``.
         - **Replace-and-own**: prior owner's XADD never landed;
           UPDATE the intent to our own bound, emit
           ``stream.deadletter_orphan_lost``.
         - **Abort**: fresh other-owner heartbeat (< 60s); return
           without mutating.

    F22 round 11+12: the FOR UPDATE SELECT may return no rows when
    another adopter completed the protocol and DELETEd the intent
    before our lock acquired. We re-enter from the top with bounded
    depth ``_attempt`` (max 3 → :class:`StreamRoutingContention`).
    """
    prom_stream = _normalize_metric_label(
        normalize_bist_ticks_label(stream),
        _KNOWN_STREAMS,
    )

    # 1. Try fresh INSERT.
    inserted = await session.execute(
        text(
            """
            INSERT INTO streams.deadletter_xadd_intent (
                failure_id, stream_name, intent_at,
                redis_lower_bound_id, owner_id, owner_heartbeat_at
            ) VALUES (
                :fid, :stream, now(), :lb, :owner, now()
            )
            ON CONFLICT (failure_id) DO NOTHING
            RETURNING failure_id
            """,
        ),
        {
            "fid": fid,
            "stream": stream,
            "lb": my_lower_bound,
            "owner": my_owner_id,
        },
    )
    if inserted.first() is not None:
        aslan_stream_deadletter_intent_adopted_total.labels(
            stream=prom_stream,
            action="own",
        ).inc()
        return AdoptionResult(action="own", lower_bound=my_lower_bound)

    # 2. ON CONFLICT fired — lock the existing row.
    existing_proxy = await session.execute(
        text(
            """
            SELECT owner_id, redis_lower_bound_id, owner_heartbeat_at,
                   EXTRACT(EPOCH FROM (now() - owner_heartbeat_at))
                       AS age_seconds
            FROM streams.deadletter_xadd_intent
            WHERE failure_id = :fid
            FOR UPDATE
            """,
        ),
        {"fid": fid},
    )
    existing = existing_proxy.one_or_none()

    if existing is None:
        # F22: another adopter reconciled + DELETEd between our
        # INSERT and our SELECT. Retry the procedure with a bounded
        # depth.
        new_attempt = _attempt + 1
        if new_attempt > 3:
            raise StreamRoutingContention(
                failure_id=fid,
                attempts=new_attempt,
            )
        return await acquire_or_adopt_intent(
            session,
            redis,
            fid=fid,
            my_owner_id=my_owner_id,
            my_lower_bound=my_lower_bound,
            stream=stream,
            _attempt=new_attempt,
        )

    # 2a. Same-owner retry — refresh heartbeat; preserve the existing
    # bound (covers any XADD this worker already issued under that
    # bound).
    if existing.owner_id == my_owner_id:
        await session.execute(
            text(
                "UPDATE streams.deadletter_xadd_intent "
                "SET owner_heartbeat_at = now() "
                "WHERE failure_id = :fid"
            ),
            {"fid": fid},
        )
        aslan_stream_deadletter_intent_adopted_total.labels(
            stream=prom_stream,
            action="own",
        ).inc()
        return AdoptionResult(
            action="own",
            lower_bound=existing.redis_lower_bound_id,
        )

    # 2b. Different worker AND fresh heartbeat — abort.
    if existing.age_seconds is not None and float(existing.age_seconds) < 60:
        aslan_stream_deadletter_intent_adopted_total.labels(
            stream=prom_stream,
            action="abort",
        ).inc()
        return AdoptionResult(action="abort", lower_bound=None)

    # 2c. Different worker AND stale heartbeat — STALE ADOPTION.
    # Walk the prior owner's redis_lower_bound_id BEFORE any column
    # rewrite (codex F21 — this is the load-bearing invariant).
    orphan_msg_id = await find_orphan_in_deadletter_stream(
        redis,
        stream,
        lower_bound=existing.redis_lower_bound_id,
        target_failure_id=fid,
    )

    if orphan_msg_id is not None:
        # 2c-i. Prior owner's XADD landed; routing is already complete.
        await session.execute(
            text(
                "INSERT INTO streams.deadletter_redis_index "
                "(failure_id, redis_message_id) "
                "VALUES (:fid, :rid) "
                "ON CONFLICT DO NOTHING"
            ),
            {"fid": fid, "rid": orphan_msg_id},
        )
        canonical_proxy = await session.execute(
            text(
                "SELECT redis_message_id "
                "FROM streams.deadletter_redis_index "
                "WHERE failure_id = :fid"
            ),
            {"fid": fid},
        )
        canonical = canonical_proxy.scalar_one()
        await session.execute(
            text(
                "UPDATE streams.deadletter_log "
                "SET redis_message_id = :rid, routed_at_redis = now() "
                "WHERE failure_id = :fid AND routed_at_redis IS NULL"
            ),
            {"fid": fid, "rid": canonical},
        )
        await session.execute(
            text("DELETE FROM streams.deadletter_xadd_intent WHERE failure_id = :fid"),
            {"fid": fid},
        )
        await audit_record(
            session,
            record=AuditRecord(
                operation="stream.deadletter_orphan_reconciled",
                target_schema="streams",
                target_table="deadletter_log",
                target_pk={"failure_id": fid},
                before=None,
                after=None,
                metadata={
                    "redis_message_id": canonical,
                    "via": "stale_adoption",
                    "prior_owner": existing.owner_id,
                    "prior_lower_bound": existing.redis_lower_bound_id,
                },
            ),
        )
        aslan_stream_deadletter_intent_adopted_total.labels(
            stream=prom_stream,
            action="reconciled_by_us",
        ).inc()
        return AdoptionResult(
            action="reconciled_by_us",
            lower_bound=None,
            redis_message_id=canonical,
        )

    # 2c-ii. Prior owner's XADD never landed — replace-and-own.
    await session.execute(
        text(
            "UPDATE streams.deadletter_xadd_intent "
            "SET owner_id = :owner, owner_heartbeat_at = now(), "
            "    intent_at = now(), redis_lower_bound_id = :lb "
            "WHERE failure_id = :fid"
        ),
        {"fid": fid, "owner": my_owner_id, "lb": my_lower_bound},
    )
    await audit_record(
        session,
        record=AuditRecord(
            operation="stream.deadletter_orphan_lost",
            target_schema="streams",
            target_table="deadletter_log",
            target_pk={"failure_id": fid},
            before=None,
            after=None,
            metadata={
                "action": "lost",
                "via": "stale_adoption",
                "prior_owner": existing.owner_id,
                "prior_lower_bound": existing.redis_lower_bound_id,
            },
        ),
    )
    aslan_stream_deadletter_orphans_lost_total.labels(
        stream=prom_stream,
    ).inc()
    aslan_stream_deadletter_intent_adopted_total.labels(
        stream=prom_stream,
        action="own",
    ).inc()
    return AdoptionResult(action="own", lower_bound=my_lower_bound)


# ── route_to_deadletter — the 6-step protocol ──────────────────────


async def route_to_deadletter(
    *,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    stream: str,
    group: str,
    consumer_name: str,
    message_id: str,
    event_id: UUID,
    failure_count: int,
    last_error: str,
    payload_excerpt: str,
    owner_id: str,
) -> RoutingResult:
    """Drive the dead-letter routing protocol from spec §7.

    Steps (all idempotent / crash-safe):

    1. ``INSERT INTO streams.deadletter_log ON CONFLICT DO UPDATE
       SET failure_count = streams.deadletter_log.failure_count
       RETURNING failure_id, redis_message_id, (xmax = 0) AS is_first_routing``
       (codex F18 round 8).
    2. ``SELECT redis_message_id FROM streams.deadletter_redis_index
       WHERE failure_id = :fid`` — if found, skip to step 4 (codex F9).
    2.4. Capture ``redis_lower_bound_id`` via ``XINFO STREAM
         <stream>.deadletter`` last-entry (codex F17).
    2.5. Call :func:`acquire_or_adopt_intent` (codex F21).
    3. XADD with auto-generated Redis ID + IMMEDIATE INSERT INTO
       ``streams.deadletter_redis_index`` (codex F5 + F9). Atomic in
       the SAME Postgres transaction.
    3.5. DELETE intent (atomic with index INSERT).
    4. UPDATE ``streams.deadletter_log.redis_message_id`` +
       ``routed_at_redis``. If 0 rows: lost-race → XDEL + DELETE
       index row; reuse the winner's ``redis_message_id``.
    5. (handled by caller via the returned :class:`RoutingResult`).
    6. Emit ``stream.deadletter`` audit (skipped on
       ``RoutingReconciledByAdoption``).
    """
    deadletter_stream = f"{stream}.deadletter"
    prom_stream = _normalize_metric_label(
        normalize_bist_ticks_label(stream),
        _KNOWN_STREAMS,
    )
    truncated_error = (last_error or "")[:8000]

    # Step 1 — durable log INSERT (idempotent).
    async with session_factory() as session:
        row_proxy = await session.execute(
            text(
                """
                INSERT INTO streams.deadletter_log (
                    stream_name, group_name, original_message_id,
                    deadletter_stream, event_id, consumer_name,
                    failure_count, last_error, payload_excerpt
                )
                VALUES (
                    :stream, :group, :mid,
                    :dl, :eid, :cname,
                    :fc, :err, CAST(:payload AS JSONB)
                )
                ON CONFLICT (stream_name, group_name, original_message_id)
                    DO UPDATE
                    SET failure_count = streams.deadletter_log.failure_count
                RETURNING failure_id, redis_message_id,
                    (xmax = 0) AS is_first_routing
                """,
            ),
            {
                "stream": stream,
                "group": group,
                "mid": message_id,
                "dl": deadletter_stream,
                "eid": str(event_id),
                "cname": consumer_name,
                "fc": failure_count,
                "err": truncated_error,
                "payload": _coerce_jsonb_str(payload_excerpt),
            },
        )
        row = row_proxy.one()
        failure_id = int(row.failure_id)
        existing_rid = row.redis_message_id
        await session.commit()

    if existing_rid is not None:
        # Step 1 resume: prior attempt already completed step 4.
        # Nothing else to do; caller XACKs.
        return RoutingComplete(redis_message_id=str(existing_rid))

    # Step 2 — Postgres-side index lookup (codex F9).
    async with session_factory() as session:
        idx_proxy = await session.execute(
            text(
                "SELECT redis_message_id FROM streams.deadletter_redis_index "
                "WHERE failure_id = :fid"
            ),
            {"fid": failure_id},
        )
        idx_rid = idx_proxy.scalar_one_or_none()

    if idx_rid is not None:
        # Step 2 hit — prior worker XADDed + index INSERTed but the
        # log UPDATE didn't run. Skip to step 4.
        aslan_stream_deadletter_index_recovered_total.labels(
            stream=prom_stream,
        ).inc()
        return await _step_4_update_log_and_audit(
            session_factory=session_factory,
            redis=redis,
            stream=stream,
            group=group,
            failure_id=failure_id,
            event_id=event_id,
            redis_message_id=str(idx_rid),
            our_xadd=False,
            prom_stream=prom_stream,
        )

    # Step 2.4 — capture redis_lower_bound_id (codex F17).
    redis_lower_bound = await _capture_redis_lower_bound(
        redis,
        deadletter_stream,
    )

    # Step 2.5 — acquire_or_adopt_intent (codex F21).
    async with session_factory() as session:
        adoption = await acquire_or_adopt_intent(
            session,
            redis,
            fid=failure_id,
            my_owner_id=owner_id,
            my_lower_bound=redis_lower_bound,
            stream=stream,
        )
        await session.commit()

    if adoption.action == "abort":
        return RoutingAborted()
    if adoption.action == "reconciled_by_us":
        # acquire_or_adopt_intent already INSERTed the index, UPDATEd
        # the log, DELETEd the intent, and emitted the reconciled
        # audit. Caller XACKs without emitting stream.deadletter.
        assert adoption.redis_message_id is not None
        return RoutingReconciledByAdoption(
            redis_message_id=str(adoption.redis_message_id),
        )

    # adoption.action == "own" — proceed with steps 3, 3.5, 4.
    # Step 3 — XADD with auto-id + IMMEDIATE index INSERT
    # + Step 3.5 — DELETE intent (atomic in same tx).
    xadd_fields = {
        "event_id": str(event_id),
        "failure_id": str(failure_id),
        "stream_name": stream,
        "group_name": group,
        "consumer_name": consumer_name,
        "failure_count": str(failure_count),
        "last_error": truncated_error,
        "payload": payload_excerpt,
    }
    redis_message_id = _decode(
        await _aw(redis.xadd(deadletter_stream, cast(Any, xadd_fields))),
    )

    async with session_factory() as session:
        # Index INSERT — runs against the CURRENT route's worker.
        try:
            await session.execute(
                text(
                    "INSERT INTO streams.deadletter_redis_index "
                    "(failure_id, redis_message_id) "
                    "VALUES (:fid, :rid)"
                ),
                {"fid": failure_id, "rid": redis_message_id},
            )
        except SQLAlchemyError:
            # Post-migration 0025 the only conflict source is the PK
            # on failure_id — a concurrent worker for the SAME failure_id
            # winning the race after our XADD. The pre-0025 path also
            # triggered on cross-stream <ms>-<seq> collisions against
            # a since-removed global UNIQUE on redis_message_id, which
            # caused silent dead-letter loss when XDEL'd here. Now safe.
            # Roll back our XADD, delete our (potentially-inserted)
            # index row, and read the winner's redis_message_id.
            await session.rollback()
            try:
                await _aw(redis.xdel(deadletter_stream, redis_message_id))
            except RedisError:  # pragma: no cover — defensive
                _log.debug("XDEL on rollback failed", exc_info=True)
            aslan_stream_deadletter_xadd_rollback_total.labels(
                stream=prom_stream,
            ).inc()
            # Re-read the winner's redis_message_id.
            async with session_factory() as recheck:
                winner_proxy = await recheck.execute(
                    text(
                        "SELECT redis_message_id FROM "
                        "streams.deadletter_redis_index "
                        "WHERE failure_id = :fid"
                    ),
                    {"fid": failure_id},
                )
                winner_rid = winner_proxy.scalar_one_or_none()
            if winner_rid is None:
                # Inconsistent state — should not happen, but guard.
                raise
            return await _step_4_update_log_and_audit(
                session_factory=session_factory,
                redis=redis,
                stream=stream,
                group=group,
                failure_id=failure_id,
                event_id=event_id,
                redis_message_id=str(winner_rid),
                our_xadd=False,
                prom_stream=prom_stream,
            )
        # Step 3.5 — DELETE intent.
        await session.execute(
            text("DELETE FROM streams.deadletter_xadd_intent WHERE failure_id = :fid"),
            {"fid": failure_id},
        )
        await session.commit()

    # Step 4 + 6.
    return await _step_4_update_log_and_audit(
        session_factory=session_factory,
        redis=redis,
        stream=stream,
        group=group,
        failure_id=failure_id,
        event_id=event_id,
        redis_message_id=redis_message_id,
        our_xadd=True,
        prom_stream=prom_stream,
        had_lost_branch_recovered=False,
    )


async def _step_4_update_log_and_audit(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    stream: str,
    group: str,
    failure_id: int,
    event_id: UUID,
    redis_message_id: str,
    our_xadd: bool,
    prom_stream: str,
    had_lost_branch_recovered: bool = False,
) -> RoutingResult:
    """Run step 4 (race-detector UPDATE on the log row) + step 6
    (``stream.deadletter`` audit + counter).

    If the UPDATE returns 0 rows, another worker already populated
    ``redis_message_id`` for this ``failure_id``. We roll back our
    XADD via XDEL + DELETE FROM index (only if ``our_xadd``); the
    caller XACKs against the winner's row.
    """
    deadletter_stream = f"{stream}.deadletter"
    async with session_factory() as session:
        upd = cast(
            "CursorResult[tuple[object, ...]]",
            await session.execute(
                text(
                    "UPDATE streams.deadletter_log "
                    "SET redis_message_id = :rid, routed_at_redis = now() "
                    "WHERE failure_id = :fid AND routed_at_redis IS NULL"
                ),
                {"fid": failure_id, "rid": redis_message_id},
            ),
        )
        if upd.rowcount == 0:
            # Lost the race. Find the winner's rid; XDEL + delete our
            # index row if we XADDed.
            winner_proxy = await session.execute(
                text("SELECT redis_message_id FROM streams.deadletter_log WHERE failure_id = :fid"),
                {"fid": failure_id},
            )
            winner_rid = winner_proxy.scalar_one_or_none()
            if our_xadd and winner_rid is not None and str(winner_rid) != redis_message_id:
                await session.execute(
                    text(
                        "DELETE FROM streams.deadletter_redis_index "
                        "WHERE failure_id = :fid AND redis_message_id = :rid"
                    ),
                    {"fid": failure_id, "rid": redis_message_id},
                )
                try:
                    await _aw(redis.xdel(deadletter_stream, redis_message_id))
                except RedisError:  # pragma: no cover — defensive
                    _log.debug("XDEL on rollback failed", exc_info=True)
                aslan_stream_deadletter_xadd_rollback_total.labels(
                    stream=prom_stream,
                ).inc()
            await session.commit()
            assert winner_rid is not None
            return RoutingComplete(redis_message_id=str(winner_rid))

        # Step 6 — emit stream.deadletter audit.
        metadata: dict[str, str | int | bool] = {
            "event_id": str(event_id),
            "failure_id": failure_id,
            "stream_name": stream,
            "group_name": group,
            "redis_message_id": redis_message_id,
        }
        if had_lost_branch_recovered:
            metadata["lost_recovered"] = True
        await audit_record(
            session,
            record=AuditRecord(
                operation="stream.deadletter",
                target_schema="streams",
                target_table="deadletter_log",
                target_pk={"failure_id": failure_id},
                before=None,
                after=None,
                metadata=metadata,
            ),
        )
        await session.commit()

    aslan_stream_deadletter_total.labels(
        stream=prom_stream,
        group=_normalize_metric_label(group, _KNOWN_CONSUMER_GROUPS),
    ).inc()
    return RoutingComplete(redis_message_id=redis_message_id)


async def _capture_redis_lower_bound(
    redis: Redis,
    deadletter_stream: str,
) -> str:
    """``XINFO STREAM`` last-entry id, or ``"0-0"`` for an empty stream."""
    try:
        info = await redis.xinfo_stream(deadletter_stream)
    except RedisError:
        # Stream doesn't exist yet — empty.
        return "0-0"
    if not info:
        return "0-0"
    last_entry = info.get("last-entry")
    if not last_entry:
        return "0-0"
    return _decode(last_entry[0])


# ── helpers ─────────────────────────────────────────────────────────


def _coerce_jsonb_str(payload_excerpt: str) -> str:
    """Coerce ``payload_excerpt`` to a string parseable as JSONB.

    Callers pass either a raw JSON string OR a Python repr; this
    function tolerates the latter by wrapping non-JSON inputs in a
    string-literal envelope.
    """
    if not payload_excerpt:
        return "null"
    try:
        json.loads(payload_excerpt)
        return payload_excerpt
    except ValueError:
        return json.dumps(payload_excerpt)


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


__all__ = [
    "AdoptionResult",
    "RoutingAborted",
    "RoutingComplete",
    "RoutingReconciledByAdoption",
    "RoutingResult",
    "acquire_or_adopt_intent",
    "find_orphan_in_deadletter_stream",
    "redis_id_compare",
    "redis_id_increment",
    "route_to_deadletter",
]
