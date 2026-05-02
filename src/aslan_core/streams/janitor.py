"""Dead-letter janitor for the v0.5 streams subsystem.

Runs the four reconciliation passes from the v0.5 streams spec:

1. stale pre-XADD intent reconciliation via ``acquire_or_adopt_intent``;
2. stuck ``streams.deadletter_log`` re-drive;
3. Postgres index to Redis stream verification;
4. bounded stream-to-index orphan detection.

The janitor reuses the same intent/adoption procedure as
``StreamConsumer`` so orphan classification has one implementation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.audit import Actor, AuditRecord, current_actor, set_actor
from aslan_core.audit import record as audit_record
from aslan_core.observability.metrics import (
    _KNOWN_CONSUMER_GROUPS,
    _KNOWN_STREAMS,
    _normalize_metric_label,
    aslan_stream_deadletter_orphans_reconciled_total,
    aslan_stream_deadletter_total,
)
from aslan_core.streams.deadletter import (
    acquire_or_adopt_intent,
    find_orphan_in_deadletter_stream,
)
from aslan_core.streams.heartbeat import heartbeat_active_intents
from aslan_core.streams.names import normalize_bist_ticks_label

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from redis.asyncio import Redis


_log = logging.getLogger(__name__)

_JANITOR_ACTOR = Actor(
    actor_id="system:streams.deadletter_janitor",
    actor_kind="system",
)


def _aw(x: Any) -> Awaitable[Any]:
    """Cast redis-py async returns for mypy --strict."""
    return cast(Awaitable[Any], x)


def _make_owner_id() -> str:
    return f"janitor:{socket.gethostname()}:{os.getpid()}:{uuid4()}"


async def stream_deadletter_janitor(
    *,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    poll_interval_s: float = 60.0,
    pass4_window: int = 10_000,
    once: bool = False,
) -> None:
    """Run the dead-letter reconciliation daemon.

    ``once=True`` performs a single four-pass cycle and returns; this is
    the test/CLI shape. The default is a long-running loop.
    """
    previous_actor = current_actor()
    set_actor(_JANITOR_ACTOR)
    owner_id = _make_owner_id()
    heartbeat_task = asyncio.create_task(
        heartbeat_active_intents(session_factory, owner_id, interval_s=30.0)
    )
    try:
        while True:
            try:
                await _pass_1_intent_reconciliation(
                    redis=redis,
                    session_factory=session_factory,
                    owner_id=owner_id,
                )
                await _pass_2_stuck_row_scan(
                    redis=redis,
                    session_factory=session_factory,
                    owner_id=owner_id,
                )
                await _pass_3_index_to_stream_verification(
                    redis=redis,
                    session_factory=session_factory,
                )
                await _pass_4_stream_to_index_defense_in_depth(
                    redis=redis,
                    session_factory=session_factory,
                    window=pass4_window,
                )
            except (RedisError, SQLAlchemyError, ValueError, TypeError, RuntimeError):
                _log.exception("dead-letter janitor pass failed; retrying next cycle")
            if once:
                return
            await asyncio.sleep(poll_interval_s)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        set_actor(previous_actor)


async def _pass_1_intent_reconciliation(
    *,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    owner_id: str,
) -> None:
    """Reconcile stale pre-XADD intents."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT failure_id, stream_name
                    FROM streams.deadletter_xadd_intent
                    WHERE intent_at < now() - INTERVAL '5 minutes'
                      AND owner_heartbeat_at < now() - INTERVAL '60 seconds'
                    ORDER BY intent_at
                    LIMIT 100
                    """
                )
            )
        ).all()

    for row in rows:
        async with session_factory() as session:
            try:
                result = await acquire_or_adopt_intent(
                    session,
                    redis,
                    fid=int(row.failure_id),
                    my_owner_id=owner_id,
                    my_lower_bound=await _capture_redis_lower_bound(
                        redis,
                        str(row.stream_name),
                    ),
                    stream=str(row.stream_name),
                )
                prom_stream = _prom_stream(str(row.stream_name))
                if result.action == "reconciled_by_us":
                    aslan_stream_deadletter_orphans_reconciled_total.labels(
                        stream=prom_stream,
                        action="reconciled",
                    ).inc()
                elif result.action == "own":
                    await session.execute(
                        text("DELETE FROM streams.deadletter_xadd_intent WHERE failure_id = :fid"),
                        {"fid": int(row.failure_id)},
                    )
                await session.commit()
            except (RedisError, SQLAlchemyError, ValueError, TypeError, RuntimeError):
                await session.rollback()
                _log.exception(
                    "janitor pass 1 failed for failure_id=%s",
                    row.failure_id,
                )


async def _pass_2_stuck_row_scan(
    *,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    owner_id: str,
) -> None:
    """Re-drive rows whose Postgres log exists but Redis routing did not finish."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT failure_id, stream_name
                    FROM streams.deadletter_log
                    WHERE routed_at_redis IS NULL
                      AND routed_at < now() - INTERVAL '5 minutes'
                    ORDER BY routed_at
                    LIMIT 100
                    """
                )
            )
        ).all()

    for row in rows:
        async with session_factory() as session:
            try:
                result = await acquire_or_adopt_intent(
                    session,
                    redis,
                    fid=int(row.failure_id),
                    my_owner_id=owner_id,
                    my_lower_bound=await _capture_redis_lower_bound(
                        redis,
                        str(row.stream_name),
                    ),
                    stream=str(row.stream_name),
                )
                if result.action == "own":
                    await _finish_routing_after_adoption(
                        session=session,
                        redis=redis,
                        failure_id=int(row.failure_id),
                    )
                await session.commit()
            except (RedisError, SQLAlchemyError, ValueError, TypeError, RuntimeError):
                await session.rollback()
                _log.exception(
                    "janitor pass 2 failed for failure_id=%s",
                    row.failure_id,
                )


async def _pass_3_index_to_stream_verification(
    *,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify each durable index row still points at a Redis entry."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT dri.failure_id, dri.redis_message_id, dl.stream_name
                    FROM streams.deadletter_redis_index AS dri
                    JOIN streams.deadletter_log AS dl USING (failure_id)
                    ORDER BY dri.failure_id
                    LIMIT 1000
                    """
                )
            )
        ).all()

    for row in rows:
        async with session_factory() as session:
            try:
                entries = await _aw(
                    redis.xrange(
                        f"{row.stream_name}.deadletter",
                        min=str(row.redis_message_id),
                        max=str(row.redis_message_id),
                    )
                )
                if entries:
                    continue
                already_audited = (
                    await session.execute(
                        text(
                            """
                            SELECT 1
                            FROM audit.events
                            WHERE operation = 'stream.deadletter_index_orphaned_in_redis'
                              AND target_pk @> CAST(:target_pk AS JSONB)
                            LIMIT 1
                            """
                        ),
                        {"target_pk": json.dumps({"failure_id": int(row.failure_id)})},
                    )
                ).scalar_one_or_none()
                if already_audited is not None:
                    continue
                await audit_record(
                    session,
                    record=AuditRecord(
                        operation="stream.deadletter_index_orphaned_in_redis",
                        target_schema="streams",
                        target_table="deadletter_redis_index",
                        target_pk={"failure_id": int(row.failure_id)},
                        before=None,
                        after=None,
                        metadata={
                            "redis_message_id": str(row.redis_message_id),
                            "stream_name": str(row.stream_name),
                            "reason": "redis_entry_missing_postgres_index_intact",
                        },
                    ),
                )
                await session.commit()
            except (RedisError, SQLAlchemyError, ValueError, TypeError, RuntimeError):
                await session.rollback()
                _log.exception(
                    "janitor pass 3 failed for failure_id=%s",
                    row.failure_id,
                )


async def _pass_4_stream_to_index_defense_in_depth(
    *,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    window: int,
) -> None:
    """Bounded recent dead-letter stream walk for missing index rows."""
    async with session_factory() as session:
        streams = (
            (
                await session.execute(
                    text(
                        """
                    SELECT DISTINCT stream_name
                    FROM streams.deadletter_log
                    ORDER BY stream_name
                    """
                    )
                )
            )
            .scalars()
            .all()
        )

    for stream_name_obj in streams:
        stream_name = str(stream_name_obj)
        deadletter_stream = f"{stream_name}.deadletter"
        try:
            entries = await _aw(redis.xrevrange(deadletter_stream, max="+", min="-", count=window))
        except RedisError:
            continue
        for message_id_obj, fields_obj in entries:
            message_id = _decode(message_id_obj)
            fields = _decode_fields(cast(dict[object, object], fields_obj))
            fid_raw = fields.get("failure_id")
            if fid_raw is None:
                continue
            await _reconcile_pass4_entry(
                redis=redis,
                session_factory=session_factory,
                stream_name=stream_name,
                redis_message_id=message_id,
                failure_id=int(fid_raw),
            )


async def _reconcile_pass4_entry(
    *,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    stream_name: str,
    redis_message_id: str,
    failure_id: int,
) -> None:
    async with session_factory() as session:
        try:
            existing_for_failure = (
                await session.execute(
                    text(
                        "SELECT redis_message_id "
                        "FROM streams.deadletter_redis_index "
                        "WHERE failure_id = :fid"
                    ),
                    {"fid": failure_id},
                )
            ).scalar_one_or_none()
            if existing_for_failure is not None:
                if str(existing_for_failure) == redis_message_id:
                    return
                await _aw(redis.xdel(f"{stream_name}.deadletter", redis_message_id))
                await audit_record(
                    session,
                    record=AuditRecord(
                        operation="stream.deadletter_orphan_xdel",
                        target_schema="streams",
                        target_table="deadletter_redis_index",
                        target_pk={"failure_id": failure_id},
                        before=None,
                        after=None,
                        metadata={
                            "redis_message_id": redis_message_id,
                            "canonical_redis_message_id": str(existing_for_failure),
                            "stream_name": stream_name,
                            "reason": "duplicate_orphan_already_indexed",
                        },
                    ),
                )
                aslan_stream_deadletter_orphans_reconciled_total.labels(
                    stream=_prom_stream(stream_name),
                    action="xdel",
                ).inc()
                await session.commit()
                return

            log_exists = (
                await session.execute(
                    text("SELECT 1 FROM streams.deadletter_log WHERE failure_id = :fid"),
                    {"fid": failure_id},
                )
            ).scalar_one_or_none()
            if log_exists is None:
                await _aw(redis.xdel(f"{stream_name}.deadletter", redis_message_id))
                await audit_record(
                    session,
                    record=AuditRecord(
                        operation="stream.deadletter_orphan_xdel",
                        target_schema="streams",
                        target_table="deadletter_log",
                        target_pk={"failure_id": failure_id},
                        before=None,
                        after=None,
                        metadata={
                            "redis_message_id": redis_message_id,
                            "stream_name": stream_name,
                            "reason": "orphan_without_postgres_log",
                        },
                    ),
                )
            else:
                await session.execute(
                    text(
                        "INSERT INTO streams.deadletter_redis_index "
                        "(failure_id, redis_message_id) "
                        "VALUES (:fid, :rid) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"fid": failure_id, "rid": redis_message_id},
                )
                await session.execute(
                    text(
                        "UPDATE streams.deadletter_log "
                        "SET redis_message_id = :rid, routed_at_redis = now() "
                        "WHERE failure_id = :fid AND routed_at_redis IS NULL"
                    ),
                    {"fid": failure_id, "rid": redis_message_id},
                )
                await audit_record(
                    session,
                    record=AuditRecord(
                        operation="stream.deadletter_orphan_reconciled",
                        target_schema="streams",
                        target_table="deadletter_log",
                        target_pk={"failure_id": failure_id},
                        before=None,
                        after=None,
                        metadata={
                            "redis_message_id": redis_message_id,
                            "stream_name": stream_name,
                            "via": "pass4_recent_stream_scan",
                        },
                    ),
                )
                aslan_stream_deadletter_orphans_reconciled_total.labels(
                    stream=_prom_stream(stream_name),
                    action="reconciled",
                ).inc()
            await session.commit()
        except (RedisError, SQLAlchemyError, ValueError, TypeError):
            await session.rollback()
            _log.exception(
                "janitor pass 4 failed for failure_id=%s",
                failure_id,
            )


async def _resolve_canonical_orphan(
    *,
    session: AsyncSession,
    redis: Redis,
    stream_name: str,
    failure_id: int,
) -> str | None:
    """Find an existing dead-letter Redis entry for ``failure_id``.

    Returns the canonical Redis message id if a prior attempt's XADD
    survived (either durably indexed in ``streams.deadletter_redis_index``
    or only present in the dead-letter stream because the consumer
    crashed between XADD and the index INSERT). Returns ``None`` if no
    prior copy exists; the caller must XADD a fresh entry.

    When an unindexed orphan is found via the dead-letter stream
    scan, this function ALSO inserts the matching
    ``streams.deadletter_redis_index`` row. Without that insert, pass 2
    would mark routing complete with ``deadletter_log.redis_message_id``
    pointing at the orphan but no durable index row, leaving the
    Redis entry invisible to pass 3's verification — and pass 4 only
    rescues such orphans inside its bounded recent window, so an
    older orphan would stay un-indexed forever (codex round-2
    follow-up to F2).

    F2 (codex post-merge) — without this lookup, ``_pass_2_stuck_row_scan``
    XADDs a duplicate when an indexed prior copy already exists, which
    pass 4 then XDELs as an orphan and leaves ``deadletter_log`` pointing
    at a deleted Redis id.
    """
    indexed = (
        await session.execute(
            text(
                "SELECT redis_message_id FROM streams.deadletter_redis_index "
                "WHERE failure_id = :fid"
            ),
            {"fid": failure_id},
        )
    ).scalar_one_or_none()
    if indexed is not None:
        # Trust the durable index even if the underlying Redis entry has
        # since been trimmed — pass 3 emits the orphan-in-redis audit
        # for that case so an operator can intervene.
        return str(indexed)

    # No durable index row. Walk the dead-letter stream for an unindexed
    # XADD orphan (consumer crashed between XADD and the index INSERT
    # before pass 4's bounded window picked it up).
    orphan = await find_orphan_in_deadletter_stream(
        redis,
        stream_name,
        lower_bound="0-0",
        target_failure_id=failure_id,
    )
    if orphan is None:
        return None
    # Codex round-2 follow-up: index the orphan now so pass 3 can
    # verify it and pass 4 can find it after the bounded window has
    # rolled past. ``ON CONFLICT DO NOTHING`` keeps the call idempotent.
    await session.execute(
        text(
            "INSERT INTO streams.deadletter_redis_index "
            "(failure_id, redis_message_id) "
            "VALUES (:fid, :rid) "
            "ON CONFLICT DO NOTHING"
        ),
        {"fid": failure_id, "rid": orphan},
    )
    return orphan


async def _finish_routing_after_adoption(
    *,
    session: AsyncSession,
    redis: Redis,
    failure_id: int,
) -> None:
    row = (
        await session.execute(
            text(
                """
                SELECT stream_name, group_name, original_message_id, event_id,
                       failure_count, payload_excerpt, last_error
                FROM streams.deadletter_log
                WHERE failure_id = :fid
                FOR UPDATE
                """
            ),
            {"fid": failure_id},
        )
    ).one()
    stream_name = str(row.stream_name)
    group_name = str(row.group_name)

    # F2 (codex post-merge): reconcile-before-overwrite. If a prior
    # attempt's XADD survived (either indexed durably or only present
    # in the dead-letter stream because the consumer crashed before
    # the index INSERT), reuse that canonical message id. Otherwise
    # pass 2 would XADD a duplicate; pass 4 would later XDEL the
    # mismatched one and leave deadletter_log pointing at a deleted
    # Redis entry.
    canonical_id = await _resolve_canonical_orphan(
        session=session,
        redis=redis,
        stream_name=stream_name,
        failure_id=failure_id,
    )
    if canonical_id is None:
        xadd_fields = {
            "event_id": str(row.event_id),
            "failure_id": str(failure_id),
            "stream_name": stream_name,
            "group_name": group_name,
            "failure_count": str(row.failure_count),
            "last_error": str(row.last_error or ""),
            "payload": json.dumps(row.payload_excerpt),
            "recovered_by": "stream_deadletter_janitor",
        }
        canonical_id = _decode(
            await _aw(redis.xadd(f"{stream_name}.deadletter", cast(Any, xadd_fields)))
        )
        await session.execute(
            text(
                "INSERT INTO streams.deadletter_redis_index "
                "(failure_id, redis_message_id) "
                "VALUES (:fid, :rid) "
                "ON CONFLICT DO NOTHING"
            ),
            {"fid": failure_id, "rid": canonical_id},
        )

    redis_message_id = canonical_id
    await session.execute(
        text(
            "UPDATE streams.deadletter_log "
            "SET redis_message_id = :rid, routed_at_redis = now() "
            "WHERE failure_id = :fid AND routed_at_redis IS NULL"
        ),
        {"fid": failure_id, "rid": redis_message_id},
    )
    await session.execute(
        text("DELETE FROM streams.deadletter_xadd_intent WHERE failure_id = :fid"),
        {"fid": failure_id},
    )

    had_lost_audit = (
        await session.execute(
            text(
                """
                SELECT 1
                FROM audit.events
                WHERE operation = 'stream.deadletter_orphan_lost'
                  AND target_pk @> CAST(:target_pk AS JSONB)
                LIMIT 1
                """
            ),
            {"target_pk": json.dumps({"failure_id": failure_id})},
        )
    ).scalar_one_or_none()
    if had_lost_audit is not None:
        await audit_record(
            session,
            record=AuditRecord(
                operation="stream.deadletter_orphan_lost_recovered",
                target_schema="streams",
                target_table="deadletter_log",
                target_pk={"failure_id": failure_id},
                before=None,
                after=None,
                metadata={
                    "redis_message_id": redis_message_id,
                    "stream_name": stream_name,
                    "via": "janitor_pass2_redrive",
                },
            ),
        )

    await audit_record(
        session,
        record=AuditRecord(
            operation="stream.deadletter",
            target_schema="streams",
            target_table="deadletter_log",
            target_pk={"failure_id": failure_id},
            before=None,
            after=None,
            metadata={
                "event_id": str(row.event_id),
                "failure_id": failure_id,
                "stream_name": stream_name,
                "group_name": group_name,
                "redis_message_id": redis_message_id,
                "recovered_by_janitor": True,
            },
        ),
    )
    aslan_stream_deadletter_total.labels(
        stream=_prom_stream(stream_name),
        group=_normalize_metric_label(group_name, _KNOWN_CONSUMER_GROUPS),
    ).inc()


async def _capture_redis_lower_bound(redis: Redis, stream_name: str) -> str:
    try:
        info = await _aw(redis.xinfo_stream(f"{stream_name}.deadletter"))
    except RedisError:
        return "0-0"
    if not info:
        return "0-0"
    last_entry = info.get("last-entry")
    if not last_entry:
        return "0-0"
    return _decode(last_entry[0])


def _prom_stream(stream_name: str) -> str:
    return _normalize_metric_label(
        normalize_bist_ticks_label(stream_name),
        _KNOWN_STREAMS,
    )


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _decode_fields(fields: dict[object, object]) -> dict[str, str]:
    return {_decode(k): _decode(v) for k, v in fields.items()}


__all__ = ["stream_deadletter_janitor"]
