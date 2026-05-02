"""``aslan streams`` Click group — operator-facing CLI for the v0.5.0
streams subsystem.

Mirrors the structure of ``aslan ts`` and ``aslan doc``: a top-level
``streams`` group with one Click command per operator workflow. Every
mutating command inherits the ``cli:<user>@<host>`` Actor set by the
top-level ``aslan`` group (v0.3.0 Task 18) so audit rows carry the
correct actor identity.

Subcommands (codex spec §12):

* :command:`publish` — publish a synthetic event via :class:`StreamProducer`.
* :command:`tail`    — tail Redis stream entries (read-only XRANGE).
* :command:`drain`   — invoke :func:`drain_outbox` once.
* :command:`lag`     — print consumer-group lag (entries in PEL +
                      undelivered).
* :command:`pending` — XPENDING summary for a (stream, group).
* :command:`deadletter list`   — list ``streams.deadletter_log`` rows.
* :command:`deadletter retry`  — manually re-drive a stuck failure_id
                                via the routing protocol.
* :command:`claim-release` — operator command to release a stuck
                              ``stream:<s>:<g>:claim:<eid>`` lease.
* :command:`processed-clear` — operator command to clear a
                                ``stream:<s>:<g>:processed`` member
                                (rare; emits an audit row).
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import click
from redis.asyncio import Redis
from sqlalchemy import text

from aslan_core.audit import AuditRecord
from aslan_core.audit import record as audit_record
from aslan_core.config import Settings
from aslan_core.db.engine import create_engine
from aslan_core.db.session import create_session_factory, session_scope
from aslan_core.ingestion.run import ingestion_run
from aslan_core.streams import (
    STREAM_FOR_EVENT_KIND,
    STREAMS,
    EntityCreatedEvent,
    FilingAmendedEvent,
    FilingNewEvent,
    ObservationBatchEvent,
    StreamEntryRedactedEvent,
    StreamEvent,
    StreamProducer,
    drain_outbox,
)
from aslan_core.streams.deadletter import route_to_deadletter

# ─── Top-level group ─────────────────────────────────────────────────────


@click.group()
def streams() -> None:
    """Stream subsystem reads, queries, and operator commands."""


@streams.group("deadletter")
def deadletter_group() -> None:
    """``streams.deadletter_log`` reads + manual re-drive."""


# ─── Helpers ─────────────────────────────────────────────────────────────


def _redis_from_settings() -> Redis:
    """Build an async Redis client from environment settings."""
    s = Settings()
    return Redis.from_url(s.redis_url, decode_responses=True)


def _aw(x: Any) -> Awaitable[Any]:
    """Cast a redis-py sync-or-async return to ``Awaitable[Any]`` for
    mypy --strict."""
    return cast(Awaitable[Any], x)


def _json_or_human(payload: Any, json_flag: bool, *, human: str | None = None) -> None:
    if json_flag:
        click.echo(json.dumps(payload, default=str))
    else:
        click.echo(human if human is not None else str(payload))


_EVENT_TYPES_BY_KIND: dict[str, type[StreamEvent]] = {
    "filing.new": FilingNewEvent,
    "filing.amended": FilingAmendedEvent,
    "observation.batch": ObservationBatchEvent,
    "entity.created": EntityCreatedEvent,
    "stream.entry_redacted": StreamEntryRedactedEvent,
}


# ─── publish ────────────────────────────────────────────────────────────


@streams.command("publish")
@click.option(
    "--stream",
    "stream_name",
    default=None,
    help=(
        "Explicit stream override; otherwise resolved from event kind via STREAM_FOR_EVENT_KIND."
    ),
)
@click.option(
    "--kind",
    required=True,
    type=click.Choice(sorted(_EVENT_TYPES_BY_KIND.keys())),
    help="Event kind discriminator.",
)
@click.option(
    "--payload",
    "payload_json_str",
    required=True,
    help=(
        "JSON-encoded event payload. ``schema_version``, ``event_id``, "
        "``produced_at``, ``producer_run_id``, ``source_id`` are auto-stamped "
        "if absent."
    ),
)
@click.option("--source-id", default="cli", show_default=True, help="src.source.source_id.")
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    help="Emit a JSON object containing the outbox_id.",
)
def publish_cmd(
    stream_name: str | None,
    kind: str,
    payload_json_str: str,
    source_id: str,
    json_flag: bool,
) -> None:
    """Publish a synthetic event via :class:`StreamProducer`.

    Opens an ``ingestion_run`` so the outbox row has a real
    ``producer_run_id``. The drainer picks the row up via
    ``aslan streams drain``.
    """
    try:
        payload = json.loads(payload_json_str)
    except json.JSONDecodeError as e:
        raise click.BadParameter(f"--payload must be valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise click.BadParameter("--payload must be a JSON object")

    # Auto-stamp boilerplate fields if the operator omitted them.
    payload.setdefault("schema_version", 1)
    payload.setdefault("event_id", str(uuid4()))
    payload.setdefault("produced_at", datetime.now(UTC).isoformat())
    payload.setdefault("source_id", source_id)
    payload.setdefault("kind", kind)

    asyncio.run(
        _publish_impl(
            stream_name=stream_name,
            kind=kind,
            payload=payload,
            source_id=source_id,
            json_flag=json_flag,
        )
    )


async def _publish_impl(
    *,
    stream_name: str | None,
    kind: str,
    payload: dict[str, Any],
    source_id: str,
    json_flag: bool,
) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with (
            ingestion_run(engine, source_id=source_id, job_name="cli.streams.publish") as run,
            session_scope(factory) as s,
        ):
            payload.setdefault("producer_run_id", run.id)
            event_cls = _EVENT_TYPES_BY_KIND[kind]
            event = event_cls.model_validate(payload)
            producer = StreamProducer(session=s, ingestion_run_id=run.id)
            outbox_id = await producer.publish(event, stream=stream_name)
        result = {
            "outbox_id": outbox_id,
            "event_id": payload["event_id"],
            "stream": stream_name or STREAM_FOR_EVENT_KIND.get(kind),
            "kind": kind,
        }
        _json_or_human(
            result,
            json_flag,
            human=f"{outbox_id}\t{result['stream']}\t{result['event_id']}",
        )
    finally:
        await engine.dispose()


# ─── tail ────────────────────────────────────────────────────────────────


@streams.command("tail")
@click.argument("stream_name")
@click.option("--group", "group_name", default=None, help="(unused; reserved for future use).")
@click.option(
    "--from-id",
    "from_id",
    default="-",
    show_default=True,
    help="Lower bound for XRANGE (``-`` for first entry, or ``<id>``).",
)
@click.option("--count", type=int, default=10, show_default=True)
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON array.")
def tail_cmd(
    stream_name: str,
    group_name: str | None,
    from_id: str,
    count: int,
    json_flag: bool,
) -> None:
    """Read-only ``XRANGE`` over ``stream_name`` (no XACK, no PEL).

    Useful for inspecting recently-published events without disturbing
    consumer-group state. ``--group`` is accepted but unused (reserved
    so a future ``--ack`` opt-in doesn't change the CLI shape)."""
    asyncio.run(_tail_impl(stream_name, from_id, count, json_flag))


async def _tail_impl(
    stream_name: str,
    from_id: str,
    count: int,
    json_flag: bool,
) -> None:
    redis = _redis_from_settings()
    try:
        entries = await redis.xrange(stream_name, min=from_id, max="+", count=count)
        out: list[dict[str, Any]] = []
        for message_id, fields in entries:
            mid = message_id.decode() if isinstance(message_id, bytes) else str(message_id)
            decoded = {
                (k.decode() if isinstance(k, bytes) else str(k)): (
                    v.decode() if isinstance(v, bytes) else str(v)
                )
                for k, v in dict(fields).items()
            }
            out.append({"message_id": mid, "fields": decoded})
        if json_flag:
            click.echo(json.dumps(out, default=str))
        else:
            for r in out:
                click.echo(f"{r['message_id']}\t{r['fields'].get('event_id', '')}")
    finally:
        await redis.aclose()


# ─── drain ───────────────────────────────────────────────────────────────


@streams.command("drain")
@click.option("--limit", type=int, default=100, show_default=True, help="batch_size.")
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON object.")
def drain_cmd(limit: int, json_flag: bool) -> None:
    """Invoke :func:`drain_outbox` once (``once=True``).

    Useful for kicking the drainer manually in dev / integration. In
    production the drainer runs as a long-lived daemon; this CLI is
    NOT a substitute."""
    asyncio.run(_drain_impl(limit, json_flag))


async def _drain_impl(limit: int, json_flag: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    redis = _redis_from_settings()
    try:
        # Capture pending count before + after so the operator sees the
        # delta without scraping the gauge.
        async with factory() as s:
            pending_before = (
                await s.execute(
                    text("SELECT count(*) FROM streams.outbox WHERE published_at IS NULL")
                )
            ).scalar_one()
        await drain_outbox(
            redis=redis,
            session_factory=factory,
            once=True,
            batch_size=limit,
        )
        async with factory() as s:
            pending_after = (
                await s.execute(
                    text("SELECT count(*) FROM streams.outbox WHERE published_at IS NULL")
                )
            ).scalar_one()
        result = {
            "pending_before": int(pending_before),
            "pending_after": int(pending_after),
            "drained": max(0, int(pending_before) - int(pending_after)),
        }
        _json_or_human(
            result,
            json_flag,
            human=(
                f"pending_before={result['pending_before']}\t"
                f"pending_after={result['pending_after']}\t"
                f"drained={result['drained']}"
            ),
        )
    finally:
        await redis.aclose()
        await engine.dispose()


# ─── lag ─────────────────────────────────────────────────────────────────


@streams.command("lag")
@click.argument("stream_name")
@click.option("--group", "group_name", required=True, help="Consumer group name.")
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON object.")
def lag_cmd(stream_name: str, group_name: str, json_flag: bool) -> None:
    """Print current consumer-group lag for ``(stream, group)``.

    Lag = entries in stream past the group's last-delivered ID, plus
    PEL size. Mirrors what the ``aslan_stream_consumer_lag_seconds``
    gauge tracks; surfaced as a synchronous read for operators who
    don't have a Prometheus dashboard handy."""
    asyncio.run(_lag_impl(stream_name, group_name, json_flag))


async def _lag_impl(stream_name: str, group_name: str, json_flag: bool) -> None:
    redis = _redis_from_settings()
    try:
        groups = await redis.xinfo_groups(stream_name)
        target: dict[str, Any] | None = None
        for g in groups:
            decoded = {
                (k.decode() if isinstance(k, bytes) else str(k)): v for k, v in dict(g).items()
            }
            name = decoded.get("name")
            name_str = name.decode() if isinstance(name, bytes) else str(name)
            if name_str == group_name:
                target = decoded
                break
        if target is None:
            raise click.ClickException(
                f"consumer group {group_name!r} not found on {stream_name!r}"
            )
        result = {
            "stream": stream_name,
            "group": group_name,
            "lag": int(target.get("lag") or 0),
            "pending": int(target.get("pending") or 0),
            "consumers": int(target.get("consumers") or 0),
            "last_delivered_id": (
                target["last-delivered-id"].decode()
                if isinstance(target.get("last-delivered-id"), bytes)
                else str(target.get("last-delivered-id"))
            ),
        }
        _json_or_human(
            result,
            json_flag,
            human=(
                f"lag={result['lag']}\tpending={result['pending']}\t"
                f"consumers={result['consumers']}\t"
                f"last_delivered_id={result['last_delivered_id']}"
            ),
        )
    finally:
        await redis.aclose()


# ─── pending ─────────────────────────────────────────────────────────────


@streams.command("pending")
@click.argument("stream_name")
@click.option("--group", "group_name", required=True)
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON object.")
def pending_cmd(stream_name: str, group_name: str, json_flag: bool) -> None:
    """Print PEL summary (XPENDING) for ``(stream, group)``."""
    asyncio.run(_pending_impl(stream_name, group_name, json_flag))


async def _pending_impl(stream_name: str, group_name: str, json_flag: bool) -> None:
    redis = _redis_from_settings()
    try:
        info = await redis.xpending(stream_name, group_name)
        decoded = {
            (k.decode() if isinstance(k, bytes) else str(k)): v for k, v in dict(info).items()
        }
        result: dict[str, Any] = {
            "stream": stream_name,
            "group": group_name,
            "pending": int(decoded.get("pending") or 0),
            "min": (
                decoded["min"].decode()
                if isinstance(decoded.get("min"), bytes)
                else decoded.get("min")
            ),
            "max": (
                decoded["max"].decode()
                if isinstance(decoded.get("max"), bytes)
                else decoded.get("max")
            ),
        }
        _json_or_human(
            result,
            json_flag,
            human=(f"pending={result['pending']}\tmin={result['min']}\tmax={result['max']}"),
        )
    finally:
        await redis.aclose()


# ─── deadletter list ─────────────────────────────────────────────────────


@deadletter_group.command("list")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option(
    "--unrouted-only",
    is_flag=True,
    help="Show only rows with routed_at_redis IS NULL (stuck in step 4).",
)
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON array.")
def deadletter_list_cmd(limit: int, unrouted_only: bool, json_flag: bool) -> None:
    """List ``streams.deadletter_log`` rows ordered by routed_at desc."""
    asyncio.run(_deadletter_list_impl(limit, unrouted_only, json_flag))


async def _deadletter_list_impl(limit: int, unrouted_only: bool, json_flag: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as s:
            sql = (
                "SELECT failure_id, stream_name, group_name, original_message_id, "
                "       redis_message_id, event_id, failure_count, routed_at, "
                "       routed_at_redis, last_error "
                "FROM streams.deadletter_log "
            )
            if unrouted_only:
                sql += "WHERE routed_at_redis IS NULL "
            sql += "ORDER BY routed_at DESC LIMIT :n"
            rows = (await s.execute(text(sql), {"n": limit})).mappings().all()
        out = [
            {
                "failure_id": int(r["failure_id"]),
                "stream_name": r["stream_name"],
                "group_name": r["group_name"],
                "original_message_id": r["original_message_id"],
                "redis_message_id": r["redis_message_id"],
                "event_id": str(r["event_id"]) if r["event_id"] else None,
                "failure_count": int(r["failure_count"]),
                "routed_at": r["routed_at"].isoformat() if r["routed_at"] else None,
                "routed_at_redis": (
                    r["routed_at_redis"].isoformat() if r["routed_at_redis"] else None
                ),
                "last_error": (str(r["last_error"]) or "")[:120],
            }
            for r in rows
        ]
        if json_flag:
            click.echo(json.dumps(out, default=str))
        else:
            for r in out:
                click.echo(
                    f"{r['failure_id']}\t{r['stream_name']}\t{r['group_name']}\t"
                    f"{r['original_message_id']}\t"
                    f"redis={r['redis_message_id']}\t"
                    f"failure_count={r['failure_count']}"
                )
    finally:
        await engine.dispose()


# ─── deadletter retry ────────────────────────────────────────────────────


@deadletter_group.command("retry")
@click.argument("failure_id", type=int)
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON object.")
def deadletter_retry_cmd(failure_id: int, json_flag: bool) -> None:
    """Manually retry routing for a stuck ``failure_id``.

    Looks up the row in ``streams.deadletter_log``, then calls
    :func:`route_to_deadletter` with the same fields. The protocol is
    idempotent (codex F18) so calling on an already-routed row is a
    no-op."""
    asyncio.run(_deadletter_retry_impl(failure_id, json_flag))


async def _deadletter_retry_impl(failure_id: int, json_flag: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    redis = _redis_from_settings()
    try:
        async with session_scope(factory) as s:
            row = (
                await s.execute(
                    text(
                        """
                        SELECT stream_name, group_name, original_message_id,
                               event_id, consumer_name, failure_count,
                               last_error, payload_excerpt
                        FROM streams.deadletter_log
                        WHERE failure_id = :fid
                        """
                    ),
                    {"fid": failure_id},
                )
            ).one_or_none()
        if row is None:
            raise click.ClickException(f"failure_id={failure_id} not found")
        owner_id = f"cli:retry:{socket.gethostname()}"
        result = await route_to_deadletter(
            redis=redis,
            session_factory=factory,
            stream=str(row.stream_name),
            group=str(row.group_name),
            consumer_name=str(row.consumer_name),
            message_id=str(row.original_message_id),
            event_id=UUID(str(row.event_id)),
            failure_count=int(row.failure_count),
            last_error=str(row.last_error or ""),
            payload_excerpt=json.dumps(row.payload_excerpt) if row.payload_excerpt else "{}",
            owner_id=owner_id,
        )
        out = {
            "failure_id": failure_id,
            "result_kind": result.__class__.__name__,
            "redis_message_id": getattr(result, "redis_message_id", None),
        }
        _json_or_human(
            out,
            json_flag,
            human=(
                f"failure_id={out['failure_id']}\tresult={out['result_kind']}\t"
                f"redis={out['redis_message_id']}"
            ),
        )
    finally:
        await redis.aclose()
        await engine.dispose()


# ─── claim-release ───────────────────────────────────────────────────────


@streams.command("claim-release")
@click.argument("stream_name")
@click.option("--group", "group_name", required=True)
@click.option("--event-id", "event_id_str", required=True, help="UUID of the event_id.")
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON object.")
def claim_release_cmd(
    stream_name: str,
    group_name: str,
    event_id_str: str,
    json_flag: bool,
) -> None:
    """Operator command — release a stuck in-flight claim lease.

    Codex spec §11 F4 round-2 footnote: the in-flight claim has a
    bounded TTL so a crashed consumer's lease expires naturally. This
    CLI is for the rare case where the operator wants to force-clear
    a claim BEFORE the TTL elapses (e.g., during a rolling deploy)."""
    UUID(event_id_str)  # validate UUID shape; raises ValueError on bad input
    asyncio.run(_claim_release_impl(stream_name, group_name, event_id_str, json_flag))


async def _claim_release_impl(
    stream_name: str,
    group_name: str,
    event_id_str: str,
    json_flag: bool,
) -> None:
    redis = _redis_from_settings()
    try:
        claim_key = f"stream:{stream_name}:{group_name}:claim:{event_id_str}"
        deleted = await redis.delete(claim_key)
        out = {
            "stream": stream_name,
            "group": group_name,
            "event_id": event_id_str,
            "deleted": int(deleted),
        }
        _json_or_human(
            out,
            json_flag,
            human=f"deleted={out['deleted']}\tclaim_key={claim_key}",
        )
    finally:
        await redis.aclose()


# ─── processed-clear ─────────────────────────────────────────────────────


@streams.command("processed-clear")
@click.argument("stream_name")
@click.option("--group", "group_name", required=True)
@click.option("--event-id", "event_id_str", required=True, help="UUID of the event_id.")
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON object.")
def processed_clear_cmd(
    stream_name: str,
    group_name: str,
    event_id_str: str,
    json_flag: bool,
) -> None:
    """Operator command — remove an event_id from the ``processed`` SET.

    Use case: re-deliver a previously-acked event to the same consumer
    group during a forensic replay. Emits a ``stream.entry_redacted``
    audit row (the closest existing operation in the allow-list) with
    a ``cause='cli:processed_clear'`` metadata tag so the action is
    discoverable on the audit trail."""
    UUID(event_id_str)  # validate UUID
    asyncio.run(_processed_clear_impl(stream_name, group_name, event_id_str, json_flag))


async def _processed_clear_impl(
    stream_name: str,
    group_name: str,
    event_id_str: str,
    json_flag: bool,
) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    redis = _redis_from_settings()
    try:
        processed_key = f"stream:{stream_name}:{group_name}:processed"
        # Audit-first ordering: the docstring promises that the action is
        # discoverable on the audit trail. If the destructive SREM ran
        # before the audit row committed, any failure in between (DB blip,
        # session_scope commit failure, audit insert error) would leave
        # Redis mutated with no audit trail — a compliance hole. Commit
        # the audit row first; SREM after. The actual ``removed`` count
        # is reported on stdout but not in the audit metadata since it
        # isn't known at audit time.
        async with session_scope(factory) as s:
            await audit_record(
                s,
                record=AuditRecord(
                    operation="stream.entry_redacted",
                    target_schema="streams",
                    target_table="consumer",
                    target_pk={"event_id": event_id_str},
                    before=None,
                    after=None,
                    metadata={
                        "stream_name": stream_name,
                        "group_name": group_name,
                        "cause": "cli:processed_clear",
                    },
                ),
            )
        removed = await _aw(redis.srem(processed_key, event_id_str))
        out = {
            "stream": stream_name,
            "group": group_name,
            "event_id": event_id_str,
            "removed": int(removed),
        }
        _json_or_human(
            out,
            json_flag,
            human=f"removed={out['removed']}\tprocessed_key={processed_key}",
        )
    finally:
        await redis.aclose()
        await engine.dispose()


# ─── group registration & known-streams listing helper ──────────────────


@streams.command("known")
@click.option("--json", "json_flag", is_flag=True, help="Emit a JSON object.")
def known_cmd(json_flag: bool) -> None:
    """List the canonical stream-name allow-list (read-only).

    Mirrors :data:`aslan_core.streams.STREAMS`. Useful for confirming
    the operator knows which stream names are pinned on the Prometheus
    ``stream`` label allow-list."""
    payload = dict(STREAMS)
    if json_flag:
        click.echo(json.dumps(payload))
    else:
        for name, desc in payload.items():
            click.echo(f"{name}\t{desc}")
