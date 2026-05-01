"""XREADGROUP-based async iterator for stream consumption.

Codex spec §7. Caller drives ACK; the consumer handles parsing,
schema-version gating, the split claim/processed dedup contract, PEL
crash-recovery via XAUTOCLAIM, lag tracking, OTel link propagation,
and dead-letter routing.

Public surface:

* :class:`StreamConsumer` — XREADGROUP-driven async iterator yielding
  ``(event, ack_callable)`` pairs. Two iteration entry points:

    - :meth:`consume` — main driver (long-running daemon shape).
    - :meth:`ensure_group` — creates the consumer group if absent;
      idempotent against ``BUSYGROUP``.

The two-key claim contract (codex F1 + F4 round 2, fan-out scoped):

* ``stream:<stream>:<group>:claim:<event_id>`` — short-lived (5-min
  lease) in-flight claim; SET NX EX at yield time; cleared on caller
  success ack(), on caller exception, OR by TTL expiry on consumer
  crash.
* ``stream:<stream>:<group>:processed`` — long-lived (7-day TTL) Redis
  SET of successfully-processed ``event_id``s; SADD'd ONLY at
  successful :meth:`ack` after caller success. The
  ``stream.consume_ack`` audit row is emitted at SADD time, not at
  claim time, so the audit log records actually-processed events
  rather than speculative claims.

Loser-XACK contract (codex F-impl-1, 2026-04-29):

When two stream entries share a single ``event_id`` (drainer-retry
duplicate), two consumers in the same group can be assigned the
duplicates. Exactly one wins the SET-NX claim and yields. The other
re-checks ``processed``: if the winner has SADD'd, the loser XACKs its
own ``message_id`` without yielding so the PEL doesn't accumulate
permanent retry duplicates. If the winner is still mid-flight, the
loser leaves the message in PEL — the next redelivery sees ``processed``
at step 1 and XACKs there.

Schema-version gate (codex spec §4):

``schema_version > max_supported`` raises
:class:`StreamSchemaVersionMismatch` with ``direction='newer'``;
``< min_supported`` raises with ``direction='older'``. Default policy
is route-to-dead-letter (``deadletter_on_schema_mismatch=True``); set
``deadletter_on_schema_mismatch=False`` to surface the exception to
the caller (used by tests + by operators that want to halt on
mismatches).

OTel trace propagation (codex spec §10):

The producer-side ``traceparent`` field on the :class:`StreamEvent`
is extracted via ``opentelemetry.propagate.extract`` and added as a
:class:`trace.Link` on a per-iteration ``StreamConsumer.iterate``
span. Malformed traceparent → graceful no-link fallback; the event
still processes.

Dead-letter routing (codex F2..F22):

Caller exceptions on yielded events bump a per-message failure
counter; once it crosses ``max_attempts_before_deadletter`` (default
5) the consumer routes the event to ``<stream>.deadletter`` via the
durable Postgres-first 6-step protocol implemented in
:mod:`aslan_core.streams.deadletter`. Permanent failures (schema
mismatch, payload validation) route on the FIRST occurrence to avoid
tight retry loops on un-fixable events.

PEL handling (codex spec §7 + Task 16):

Every iteration of :meth:`consume` runs ``XAUTOCLAIM`` (idle-time
``pel_idle_claim_ms``) BEFORE the ``XREADGROUP``, so a sibling
consumer's crashed PEL entry is picked up automatically. The
``aslan_stream_consumer_lag_seconds`` gauge is updated per yielded
event as ``now() - event.produced_at``.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from redis.exceptions import RedisError, ResponseError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.audit import AuditRecord
from aslan_core.audit import record as audit_record
from aslan_core.config import Settings
from aslan_core.errors import (
    StreamPayloadValidationError,
    StreamRedactionIntegrityError,
    StreamSchemaVersionMismatch,
)
from aslan_core.observability.metrics import (
    _KNOWN_CONSUMER_GROUPS,
    _KNOWN_STREAMS,
    _normalize_metric_label,
    aslan_stream_acks_total,
    aslan_stream_consume_claim_held_elsewhere_total,
    aslan_stream_consume_dedup_skip_total,
    aslan_stream_consume_duration_seconds,
    aslan_stream_consumed_redacted_total,
    aslan_stream_consumer_lag_seconds,
    aslan_stream_consumes_total,
    aslan_stream_schema_mismatch_total,
)
from aslan_core.observability.tracing import traced
from aslan_core.streams.deadletter import (
    RoutingAborted,
    RoutingComplete,
    RoutingReconciledByAdoption,
    RoutingResult,
    route_to_deadletter,
)
from aslan_core.streams.events import KnownStreamEvent, StreamEvent
from aslan_core.streams.names import normalize_bist_ticks_label
from aslan_core.streams.redaction import (
    acquire_event_lock,
    canonical_payload_hash,
    fetch_cached_registry_entry,
    fetch_registry_entry,
)

if TYPE_CHECKING:  # pragma: no cover — typing-only import
    from redis.asyncio import Redis


_log = logging.getLogger(__name__)

_AdapterT: TypeAdapter[KnownStreamEvent] = TypeAdapter(KnownStreamEvent)


CLAIM_LEASE_SECONDS: int = 300
"""Default per-event in-flight claim lease (5 minutes). The claim
auto-expires if the consumer crashes mid-yield, freeing the event for
re-claim by a sibling."""

PROCESSED_TTL_SECONDS: int = 7 * 24 * 3600
"""Default ``stream:<stream>:<group>:processed`` SET TTL (7 days). The
TTL is refreshed on every successful ack so an actively-used consumer
group's dedup window never lapses."""


def _aw(x: Any) -> Awaitable[Any]:
    """Cast a redis-py return value to ``Awaitable[Any]``.

    redis-py's stubs declare methods as ``Union[T, Awaitable[T]]``
    because the same class is reused for sync + async. In our async
    context the return is always awaitable; this cast makes mypy
    --strict happy without relying on ``# type: ignore`` everywhere.
    """
    return cast(Awaitable[Any], x)


def _new_owner_id(prefix: str = "consumer") -> str:
    """Return a per-process worker identifier (codex F20 round 9)."""
    import os
    import socket

    return f"{prefix}:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


class StreamConsumer:
    """XREADGROUP-based async iterator with caller-driven ACK + the
    split claim/processed contract, PEL XAUTOCLAIM, OTel link
    propagation, and dead-letter routing.

    Codex spec §7 + critical-contract items 7-13.

    :param redis: Async Redis client (``redis.asyncio.Redis``).
    :param session_factory: AsyncSession factory used by the
        ack-callback's audit emission AND by the dead-letter routing
        protocol. The consumer opens a fresh session per audit emission
        so the routing transaction is isolated from the caller's.
    :param max_supported_schema_version: Highest ``schema_version`` the
        consumer can parse. Events with newer versions raise
        :class:`StreamSchemaVersionMismatch` (direction='newer').
    :param min_supported_schema_version: Lowest ``schema_version`` the
        consumer can parse. Defaults to 1.
    :param max_attempts_before_deadletter: After this many caller
        exceptions on a single ``message_id``, the event is routed to
        ``<stream>.deadletter``. Defaults to 5.
    :param pel_idle_claim_ms: XAUTOCLAIM idle threshold (default 60s).
        A sibling's PEL entry is reclaimed once it's been idle this
        long.
    :param deadletter_on_schema_mismatch: When True (default), schema
        mismatches route to dead-letter on the first occurrence rather
        than raising. Set to False in tests / operator workflows that
        want the exception to bubble.
    :param settings: Optional pre-loaded :class:`Settings`. Default
        re-reads from environment.
    :param owner_id: Optional explicit worker identifier (used by the
        dead-letter routing protocol's ownership gate). Defaults to a
        fresh ``consumer:<host>:<pid>:<uuid>`` value generated at
        construction.
    """

    def __init__(
        self,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_supported_schema_version: int,
        min_supported_schema_version: int = 1,
        max_attempts_before_deadletter: int = 5,
        pel_idle_claim_ms: int = 60_000,
        deadletter_on_schema_mismatch: bool = True,
        settings: Settings | None = None,
        owner_id: str | None = None,
    ) -> None:
        self._redis = redis
        self._session_factory = session_factory
        self.max_supported = max_supported_schema_version
        self.min_supported = min_supported_schema_version
        self.max_attempts_before_deadletter = max_attempts_before_deadletter
        self.pel_idle_claim_ms = pel_idle_claim_ms
        self.deadletter_on_schema_mismatch = deadletter_on_schema_mismatch
        self._settings = settings
        self.owner_id: str = owner_id or _new_owner_id()

    @traced("StreamConsumer.ensure_group")
    async def ensure_group(
        self,
        stream: str,
        group: str,
        *,
        start_id: str = "0",
        mkstream: bool = True,
    ) -> None:
        """Create the consumer group if absent; swallow ``BUSYGROUP``.

        ``start_id`` defaults to ``"0"`` so a newly-created group
        consumes existing stream entries. Long-running daemons that
        intentionally want only future entries can pass ``"$"``.
        """
        try:
            await self._redis.xgroup_create(
                stream,
                group,
                id=start_id,
                mkstream=mkstream,
            )
        except ResponseError as exc:  # pragma: no cover — Redis BUSYGROUP only
            if "BUSYGROUP" not in str(exc):
                raise

    async def consume(
        self,
        stream: str,
        group: str,
        consumer_name: str,
        *,
        count: int = 10,
        block_ms: int = 5_000,
    ) -> AsyncIterator[tuple[StreamEvent, Callable[[], Awaitable[None]]]]:
        """Yield ``(event, ack_callable)`` pairs from ``stream`` until cancelled.

        The caller is responsible for calling ``await ack()`` on success.
        On caller exception that surfaces through ``await ack()`` (e.g.
        Lua/audit failures), the consumer's ``except Exception:`` path
        runs: DEL the in-flight claim, increment the failure counter,
        and route to dead-letter if the threshold is crossed.

        **Lifecycle note (codex F7 — v0.5.2 follow-up):** when the
        caller's ``async for`` body raises, Python does NOT propagate
        that exception to this generator's frame — it sends
        ``GeneratorExit`` via ``aclose()``, a ``BaseException`` that
        ``except Exception:`` does not catch. The consumer therefore
        does NOT release the claim on caller-body raise; the claim
        instead expires naturally under its 5-minute TTL lease before
        a sibling consumer can pick the message back up. Releasing
        the claim synchronously on body raise is a v0.5.2 consumer-API
        redesign (the ack callable would expose a paired ``release``
        path that the framework calls under both success and failure
        without relying on ``GeneratorExit``).

        :param stream: The Redis stream name (canonical, from
            :data:`aslan_core.streams.names.STREAMS`).
        :param group: Consumer group name. Multiple consumers in the
            same group split entries; multiple groups see all entries
            (fan-out).
        :param consumer_name: Per-instance consumer identifier; used by
            Redis to track PEL ownership.
        :param count: ``XREADGROUP`` / ``XAUTOCLAIM`` batch size.
        :param block_ms: ``XREADGROUP BLOCK`` timeout in milliseconds.
        """
        prom_stream = _normalize_metric_label(
            normalize_bist_ticks_label(stream),
            _KNOWN_STREAMS,
        )
        prom_group = _normalize_metric_label(group, _KNOWN_CONSUMER_GROUPS)

        autoclaim_cursor = "0-0"

        while True:
            # Step A — XAUTOCLAIM (codex spec §7 step 2 + Task 16):
            # pick up idle PEL entries from crashed siblings BEFORE
            # reading new ones. The cursor advances per-call so we
            # cycle through the PEL even on long-running consumers.
            try:
                claim_result = await self._redis.xautoclaim(
                    stream,
                    group,
                    consumer_name,
                    min_idle_time=self.pel_idle_claim_ms,
                    start_id=autoclaim_cursor,
                    count=count,
                )
                # redis-py returns (next_cursor, claimed_entries, deleted_ids)
                # in 5.x; older versions return (next_cursor, claimed_entries).
                next_cursor: Any = claim_result[0]
                claimed_entries: list[Any] = list(claim_result[1])
                autoclaim_cursor = (
                    next_cursor.decode() if isinstance(next_cursor, bytes) else str(next_cursor)
                )
            except RedisError:
                _log.exception(
                    "XAUTOCLAIM failed on %s/%s; continuing to XREADGROUP",
                    stream,
                    group,
                )
                claimed_entries = []

            for message_id, fields in claimed_entries:
                async for item in self._process_message(
                    stream=stream,
                    group=group,
                    consumer_name=consumer_name,
                    message_id=_decode_id(message_id),
                    fields=_decode_fields(fields),
                    prom_stream=prom_stream,
                    prom_group=prom_group,
                ):
                    yield item

            # Step B — XREADGROUP for fresh entries.
            messages = await self._redis.xreadgroup(
                groupname=group,
                consumername=consumer_name,
                streams={stream: ">"},
                count=count,
                block=block_ms,
            )
            if not messages:
                continue

            for _stream_name, entries in messages:
                for message_id, fields in entries:
                    async for item in self._process_message(
                        stream=stream,
                        group=group,
                        consumer_name=consumer_name,
                        message_id=_decode_id(message_id),
                        fields=_decode_fields(fields),
                        prom_stream=prom_stream,
                        prom_group=prom_group,
                    ):
                        yield item

    async def _process_message(
        self,
        *,
        stream: str,
        group: str,
        consumer_name: str,
        message_id: str,
        fields: dict[str, str],
        prom_stream: str,
        prom_group: str,
    ) -> AsyncIterator[tuple[StreamEvent, Callable[[], Awaitable[None]]]]:
        """Process one Redis stream entry; yield it to the caller iff
        all gates pass.

        The body is split out so XAUTOCLAIM and XREADGROUP can share it.
        """
        t0 = time.perf_counter()
        aslan_stream_consumes_total.labels(
            stream=prom_stream,
            group=prom_group,
        ).inc()

        # Step A — parse + schema gate.
        try:
            event = self._parse_or_raise(stream, message_id, fields)
        except StreamPayloadValidationError as exc:
            await self._handle_permanent_failure(
                stream=stream,
                group=group,
                consumer_name=consumer_name,
                message_id=message_id,
                event_id=exc.event_id,
                fields=fields,
                error=exc,
            )
            return

        try:
            self._check_schema_version(event, stream, prom_stream)
        except StreamSchemaVersionMismatch as exc:
            if self.deadletter_on_schema_mismatch:
                await self._handle_permanent_failure(
                    stream=stream,
                    group=group,
                    consumer_name=consumer_name,
                    message_id=message_id,
                    event_id=event.event_id,
                    fields=fields,
                    error=exc,
                )
                return
            raise

        # Lag gauge.
        try:
            lag = (datetime.now(UTC) - event.produced_at).total_seconds()
            aslan_stream_consumer_lag_seconds.labels(
                stream=prom_stream,
                group=prom_group,
            ).set(lag)
        except (AttributeError, RuntimeError, ValueError):  # pragma: no cover — defensive
            _log.debug("failed to update lag gauge", exc_info=True)

        processed_key = f"stream:{stream}:{group}:processed"
        claim_key = f"stream:{stream}:{group}:claim:{event.event_id}"

        # Step 1 — early redaction lookup. This is a performance hint
        # only; the authoritative compliance check runs under the
        # advisory lock immediately before yield.
        async with self._session_factory() as early_session:
            early_redaction = await fetch_cached_registry_entry(
                self._redis,
                early_session,
                event.event_id,
            )
        redacted_candidate = (
            self._event_from_registry(
                stream=stream,
                original_event_id=event.event_id,
                payload=early_redaction.redacted_payload,
                payload_hash=early_redaction.redacted_payload_hash,
            )
            if early_redaction is not None
            else None
        )

        # Step 2 — completed-dedup gate.
        if await _aw(self._redis.sismember(processed_key, str(event.event_id))):
            await _aw(self._redis.xack(stream, group, message_id))
            aslan_stream_consume_dedup_skip_total.labels(
                stream=prom_stream,
                group=prom_group,
            ).inc()
            return

        # Step 3 — atomic in-flight claim.
        claim_payload = json.dumps(
            {
                "owner": consumer_name,
                "message_id": message_id,
                "claimed_at": datetime.now(UTC).isoformat(),
            }
        )
        claimed = await self._redis.set(
            claim_key,
            claim_payload,
            nx=True,
            ex=CLAIM_LEASE_SECONDS,
        )
        if not claimed:
            existing_raw = await _aw(self._redis.get(claim_key))
            existing = _decode_str(existing_raw)
            if existing:
                try:
                    owner = json.loads(existing).get("owner")
                except json.JSONDecodeError:
                    owner = None
                if owner == consumer_name:
                    # Same-consumer re-yield under the existing lease;
                    # fall through to step 3.
                    pass
                else:
                    # Codex F-impl-1: loser path. If the winner has
                    # already SADD'd, XACK without yielding to drain
                    # the duplicate from the PEL. Otherwise leave it
                    # in PEL — the next redelivery will see processed
                    # at step 1.
                    if await _aw(
                        self._redis.sismember(
                            processed_key,
                            str(event.event_id),
                        ),
                    ):
                        await _aw(self._redis.xack(stream, group, message_id))
                        aslan_stream_consume_dedup_skip_total.labels(
                            stream=prom_stream,
                            group=prom_group,
                        ).inc()
                    else:
                        aslan_stream_consume_claim_held_elsewhere_total.labels(
                            stream=prom_stream,
                            group=prom_group,
                        ).inc()
                    return
            else:
                # Race: TTL expired between SET and GET; skip this
                # iteration and rely on Redis re-delivery.
                return

        # Step 4 — lock, authoritatively re-check, set up trace link,
        # ack callback, and yield.
        lock_session = self._session_factory()
        try:
            await acquire_event_lock(lock_session, event.event_id)
            authoritative_redaction = await fetch_registry_entry(
                lock_session,
                event.event_id,
            )
            is_redacted = authoritative_redaction is not None or redacted_candidate is not None
            if authoritative_redaction is not None:
                event = self._event_from_registry(
                    stream=stream,
                    original_event_id=event.event_id,
                    payload=authoritative_redaction.redacted_payload,
                    payload_hash=authoritative_redaction.redacted_payload_hash,
                )
            elif redacted_candidate is not None:
                event = redacted_candidate
        except (
            RedisError,
            SQLAlchemyError,
            StreamPayloadValidationError,
            StreamRedactionIntegrityError,
        ):
            await _aw(self._redis.delete(claim_key))
            await lock_session.rollback()
            await lock_session.close()
            raise

        producer_ctx = _extract_producer_context(event.traceparent)

        # Build the ack callback (closes over message_id + event).
        captured_event_id = event.event_id
        captured_message_id = message_id
        captured_is_redacted = is_redacted
        lock_released = False

        async def _ack() -> None:
            """Caller-success transition: audit-then-Lua.

            F4 (codex post-merge): the audit row is written and committed
            BEFORE the irreversible Redis transition (SADD processed +
            DEL claim + XACK), so a Redis-side failure after this point
            leaves a Postgres audit row rather than a silent compliance
            hole. The trade-off is at-least-once delivery if the Lua
            call fails after the audit commits — the message stays in
            PEL, the next consumer re-yields to the caller (the
            ``processed`` SADD never happened), and a second consume
            audit row records the redelivery. Operators see the duplicate
            audit instead of losing the consume signal entirely.
            """
            nonlocal lock_released
            await audit_record(
                lock_session,
                record=AuditRecord(
                    operation=(
                        "stream.consumed_redacted" if captured_is_redacted else "stream.consume_ack"
                    ),
                    target_schema="streams",
                    target_table="consumer",
                    target_pk={"event_id": str(captured_event_id)},
                    before=None,
                    after=None,
                    metadata={
                        "event_id": str(captured_event_id),
                        "stream_name": stream,
                        "group_name": group,
                        "consumer_name": consumer_name,
                        "redis_message_id": captured_message_id,
                        "redacted": captured_is_redacted,
                    },
                ),
            )
            await lock_session.commit()
            await lock_session.close()
            lock_released = True
            lua = (
                "redis.call('SADD', KEYS[1], ARGV[1])\n"
                "redis.call('EXPIRE', KEYS[1], ARGV[2])\n"
                "redis.call('DEL', KEYS[2])\n"
                "redis.call('XACK', KEYS[3], ARGV[3], ARGV[4])\n"
                "return 1\n"
            )
            await _aw(
                self._redis.eval(
                    lua,
                    3,
                    processed_key,
                    claim_key,
                    stream,
                    str(captured_event_id),
                    str(PROCESSED_TTL_SECONDS),
                    group,
                    captured_message_id,
                ),
            )
            aslan_stream_acks_total.labels(
                stream=prom_stream,
                group=prom_group,
            ).inc()
            if captured_is_redacted:
                aslan_stream_consumed_redacted_total.labels(
                    stream=prom_stream,
                    group=prom_group,
                ).inc()

        # Step 4 — yield to caller within an iteration span (codex
        # spec §10). Exceptions inside the caller's body propagate;
        # the consumer's exception path tracks failure count and may
        # route to dead-letter. NOTE: when the caller's ``async for``
        # body raises, Python does NOT propagate that exception to
        # this generator's frame — it calls ``gen.aclose()`` which
        # raises ``GeneratorExit``, a ``BaseException`` not caught by
        # ``except Exception:``. We deliberately do NOT catch it here:
        # the cleanup-via-Redis path interacts poorly with redis-py
        # connection lifecycle during pytest teardown (ResourceWarnings
        # surface as next-test errors). The claim instead expires
        # naturally under its 5-minute TTL lease, and the F4 PEL
        # contract above is what carries correctness. A v0.5.2
        # consumer-API redesign (release the claim synchronously via
        # the ack-failure path) is the proper fix for codex F7.
        try:
            async for item in self._yield_with_iterate_span(
                event=event,
                ack_callable=_ack,
                stream=stream,
                group=group,
                producer_ctx=producer_ctx,
            ):
                yield item
        except Exception as exc:
            tb = traceback.format_exc()[:4000]
            await self._handle_caller_exception(
                stream=stream,
                group=group,
                consumer_name=consumer_name,
                message_id=message_id,
                event=event,
                fields=fields,
                claim_key=claim_key,
                error=exc,
                last_error=tb,
            )
            await lock_session.rollback()
            await lock_session.close()
            lock_released = True
            raise
        finally:
            if not lock_released:
                await lock_session.rollback()
                await lock_session.close()
            elapsed = time.perf_counter() - t0
            try:
                aslan_stream_consume_duration_seconds.labels(
                    stream=prom_stream,
                ).observe(elapsed)
            except (AttributeError, RuntimeError, ValueError):  # pragma: no cover — defensive
                _log.debug("failed to observe consume duration", exc_info=True)

    def _event_from_registry(
        self,
        *,
        stream: str,
        original_event_id: UUID,
        payload: dict[str, Any],
        payload_hash: str,
    ) -> StreamEvent:
        if canonical_payload_hash(payload) != payload_hash:
            raise StreamRedactionIntegrityError(
                event_id=original_event_id,
                stream_name=stream,
            )
        try:
            parsed = _AdapterT.validate_python(payload)
        except ValidationError as exc:
            raise StreamPayloadValidationError(
                event_id=original_event_id,
                stream_name=stream,
                message=f"redacted payload validation failed: {exc}",
            ) from exc
        if parsed.event_id != original_event_id:
            raise StreamPayloadValidationError(
                event_id=original_event_id,
                stream_name=stream,
                message="redacted payload event_id does not match registry key",
            )
        return parsed

    async def _yield_with_iterate_span(
        self,
        *,
        event: StreamEvent,
        ack_callable: Callable[[], Awaitable[None]],
        stream: str,
        group: str,
        producer_ctx: Any,
    ) -> AsyncIterator[tuple[StreamEvent, Callable[[], Awaitable[None]]]]:
        """Wrap the yield in a per-iteration ``StreamConsumer.iterate``
        span (codex spec §10). The span links to the producer's span
        when ``traceparent`` extraction succeeds; malformed traceparent
        falls through with no link."""
        try:
            from opentelemetry import trace as otel_trace
        except ImportError:
            yield event, ack_callable
            return

        tracer = otel_trace.get_tracer(__name__)
        links: list[Any] = []
        if producer_ctx is not None:
            try:
                links = [otel_trace.Link(producer_ctx)]
            except (AttributeError, RuntimeError, ValueError):  # pragma: no cover — defensive
                links = []
        with tracer.start_as_current_span(
            "StreamConsumer.iterate",
            links=links,
        ) as iter_span:
            try:
                iter_span.set_attribute("aslan.stream.name", stream)
                iter_span.set_attribute("aslan.stream.group", group)
                iter_span.set_attribute("aslan.event_id", str(event.event_id))
            except (AttributeError, RuntimeError, ValueError):  # pragma: no cover — defensive
                _log.debug("failed to set iterate-span attributes", exc_info=True)
            yield event, ack_callable

    async def _handle_caller_exception(
        self,
        *,
        stream: str,
        group: str,
        consumer_name: str,
        message_id: str,
        event: StreamEvent,
        fields: dict[str, str],
        claim_key: str,
        error: BaseException,
        last_error: str,
    ) -> None:
        """Caller-failure cleanup: release the claim, bump the failure
        counter, route to dead-letter on threshold."""
        try:
            await self._redis.delete(claim_key)
        except RedisError:  # pragma: no cover — defensive
            _log.debug("failed to release claim_key", exc_info=True)

        # F6 (codex post-merge): never silently classify a failure-counter
        # bookkeeping error as ``failures = 0``. Letting the read fall
        # through would leave a poison message cycling in PEL forever
        # because dead-letter escalation depends on the counter crossing
        # ``max_attempts_before_deadletter``. Propagate the Redis error
        # so the next redelivery retries the bump (the claim was already
        # released above, so no consumer is starved). The original caller
        # exception is preserved in ``last_error`` and reaches the
        # outer consume-loop traceback.
        failures = await _aw(
            self._redis.hincrby(
                f"stream:{stream}:{group}:failures",
                message_id,
                1,
            ),
        )

        if failures > self.max_attempts_before_deadletter:
            await self._route(
                stream=stream,
                group=group,
                consumer_name=consumer_name,
                message_id=message_id,
                event=event,
                fields=fields,
                failure_count=int(failures),
                last_error=last_error,
            )

    async def _handle_permanent_failure(
        self,
        *,
        stream: str,
        group: str,
        consumer_name: str,
        message_id: str,
        event_id: UUID | None,
        fields: dict[str, str],
        error: BaseException,
    ) -> None:
        """Schema mismatch / payload validation failure routes to
        dead-letter on the first occurrence (codex spec §4)."""
        last_error = f"{type(error).__name__}: {error}"[:4000]
        # Build a minimal event-shaped object for dead-letter routing.
        # Use the ``event_id`` we extracted (or a zero UUID for malformed).
        evid = event_id or UUID(int=0)
        await self._route(
            stream=stream,
            group=group,
            consumer_name=consumer_name,
            message_id=message_id,
            event=None,
            fields=fields,
            failure_count=self.max_attempts_before_deadletter + 1,
            last_error=last_error,
            event_id_override=evid,
        )

    async def _route(
        self,
        *,
        stream: str,
        group: str,
        consumer_name: str,
        message_id: str,
        event: StreamEvent | None,
        fields: dict[str, str],
        failure_count: int,
        last_error: str,
        event_id_override: UUID | None = None,
    ) -> None:
        """Drive the dead-letter routing protocol via
        :func:`aslan_core.streams.deadletter.route_to_deadletter`.

        Maps the routing result back onto the consumer's PEL state:
        complete → XACK; reconciled-by-adoption → XACK; aborted →
        leave in PEL.
        """
        event_id = event.event_id if event is not None else (event_id_override or UUID(int=0))
        # Build the payload-excerpt JSON for forensics (truncate to
        # ~4 KB; codex spec §7).
        try:
            payload_json = json.dumps(fields)[:4096]
        except (TypeError, ValueError):
            payload_json = ""
        result: RoutingResult = await route_to_deadletter(
            redis=self._redis,
            session_factory=self._session_factory,
            stream=stream,
            group=group,
            consumer_name=consumer_name,
            message_id=message_id,
            event_id=event_id,
            failure_count=failure_count,
            last_error=last_error,
            payload_excerpt=payload_json,
            owner_id=self.owner_id,
        )
        if isinstance(result, RoutingComplete | RoutingReconciledByAdoption):
            try:
                await self._redis.xack(stream, group, message_id)
            except RedisError:  # pragma: no cover — defensive
                _log.debug("post-route XACK failed", exc_info=True)
        elif isinstance(result, RoutingAborted):
            # Leave in PEL; the next pickup re-runs the protocol.
            return

    # ── parse + schema helpers ──────────────────────────────────────

    def _parse_or_raise(
        self,
        stream: str,
        message_id: str,
        fields: dict[str, str],
    ) -> StreamEvent:
        try:
            payload_raw = fields["payload"]
        except KeyError as exc:
            raise StreamPayloadValidationError(
                event_id=None,
                stream_name=stream,
                message=f"missing 'payload' field on stream entry {message_id}",
            ) from exc

        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError as exc:
            evid = _safe_uuid(fields.get("event_id", ""))
            raise StreamPayloadValidationError(
                event_id=evid,
                stream_name=stream,
                message=f"malformed JSON in payload: {exc}",
            ) from exc

        try:
            return _AdapterT.validate_python(payload)
        except ValidationError as exc:
            evid = _safe_uuid(fields.get("event_id", ""))
            raise StreamPayloadValidationError(
                event_id=evid,
                stream_name=stream,
                message=str(exc),
            ) from exc

    def _check_schema_version(
        self,
        event: StreamEvent,
        stream: str,
        prom_stream: str,
    ) -> None:
        if event.schema_version > self.max_supported:
            aslan_stream_schema_mismatch_total.labels(
                stream=prom_stream,
                direction="newer",
            ).inc()
            raise StreamSchemaVersionMismatch(
                event_id=event.event_id,
                stream_name=stream,
                received_version=event.schema_version,
                supported_range=(self.min_supported, self.max_supported),
                direction="newer",
            )
        if event.schema_version < self.min_supported:
            aslan_stream_schema_mismatch_total.labels(
                stream=prom_stream,
                direction="older",
            ).inc()
            raise StreamSchemaVersionMismatch(
                event_id=event.event_id,
                stream_name=stream,
                received_version=event.schema_version,
                supported_range=(self.min_supported, self.max_supported),
                direction="older",
            )


def _decode_id(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _decode_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _decode_fields(fields: Any) -> dict[str, str]:
    """Normalize XREADGROUP/XAUTOCLAIM fields into a ``dict[str, str]``.

    redis-py with ``decode_responses=True`` returns ``dict[str, str]``;
    without the flag it returns ``dict[bytes, bytes]``. We tolerate
    both.
    """
    if isinstance(fields, dict):
        out: dict[str, str] = {}
        for k, v in fields.items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            val = v.decode() if isinstance(v, bytes) else str(v)
            out[key] = val
        return out
    return {}


def _safe_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        return None


def _extract_producer_context(traceparent: str | None) -> Any:
    """Return an OTel context-like object suitable for ``trace.Link``,
    or None if extraction fails / OTel is not installed."""
    if not traceparent:
        return None
    try:
        from opentelemetry import propagate
        from opentelemetry import trace as otel_trace
    except ImportError:
        return None
    try:
        ctx = propagate.extract({"traceparent": traceparent})
        span = otel_trace.get_current_span(ctx)
        sc = span.get_span_context()
        if sc.is_valid:
            return sc
    except (AttributeError, RuntimeError, ValueError):
        _log.warning(
            "malformed traceparent %r; falling back to no-link",
            traceparent,
            exc_info=True,
        )
    return None


__all__ = [
    "CLAIM_LEASE_SECONDS",
    "PROCESSED_TTL_SECONDS",
    "StreamConsumer",
]
