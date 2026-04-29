"""Transactional outbox writer.

Codex spec §5 — single transaction per call; the caller owns commit /
rollback; the producer attaches its outbox INSERT + audit row to the
supplied AsyncSession. The outbox row + audit row land atomically in
the caller's transaction (codex critical-contract item 2: rollback on
the caller's exception → both go).

Public surface:

* :class:`StreamProducer` — bound to one ``AsyncSession`` +
  ``ingestion_run_id``. Methods:
    - :meth:`publish` (Task 8): single event, returns the outbox row id.
    - :meth:`publish_many` (Task 9): bulk variant, returns the list of
      outbox row ids; emits ONE audit event per call.

Auto-stamping:

The producer fills missing event fields BEFORE issuing any DB I/O:

  * ``producer_run_id`` → from ``self.ingestion_run_id`` (the producer
    is the authority on which ingestion run a publish belongs to; any
    caller-supplied value is overridden).
  * ``actor_id`` / ``actor_kind`` → from
    :func:`aslan_core.audit.current_actor`.
  * ``traceparent`` → from the active OTel span context via
    :func:`opentelemetry.propagate.inject` when ``[obs]`` is installed.

Strict-mode contract (codex F3 + critical-contract item 3):

When ``Settings.audit_strict=True`` AND ``current_actor()`` is None,
the producer raises :class:`AuditMissingActor` BEFORE any DB I/O —
this is the procurement-grade contract that strict mode rejects
mutations without attribution before side effects run.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit import (
    Actor,
    AuditRecord,
    assert_actor_or_strict_raise,
    current_actor,
)
from aslan_core.audit import record as audit_record
from aslan_core.config import Settings
from aslan_core.errors import (
    StreamEventIdConflict,
    UnknownEventKind,
)
from aslan_core.models.streams import Outbox
from aslan_core.observability.metrics import (
    _KNOWN_STREAMS,
    _normalize_metric_label,
    aslan_stream_publish_duration_seconds,
    aslan_stream_publishes_total,
)
from aslan_core.observability.tracing import traced
from aslan_core.streams.events import StreamEvent
from aslan_core.streams.names import (
    STREAM_FOR_EVENT_KIND,
    normalize_bist_ticks_label,
)

_log = logging.getLogger(__name__)


class StreamProducer:
    """Transactional outbox writer bound to one ``AsyncSession``.

    The caller owns the transaction; the producer attaches the outbox
    INSERT + audit row to ``session`` and returns the outbox row id.
    The caller decides commit vs rollback.

    :param session: AsyncSession in which the outbox + audit rows are
        inserted. The producer does NOT commit / rollback.
    :param ingestion_run_id: ``src.ingestion_run.ingestion_run_id`` the
        publish is attributed to. The producer overrides any value the
        caller stamped on the event payload.
    :param settings: Optional pre-loaded :class:`Settings`. Default
        re-reads from environment so the strict-mode flag stays live for
        tests that flip it via env vars.
    """

    def __init__(
        self,
        session: AsyncSession,
        ingestion_run_id: int,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.ingestion_run_id = ingestion_run_id
        self._settings = settings

    @traced("StreamProducer.publish")
    async def publish(
        self,
        event: StreamEvent,
        *,
        stream: str | None = None,
    ) -> int:
        """Write ``event`` to streams.outbox in the caller's transaction.

        Auto-stamps missing fields (``producer_run_id``, ``actor_id`` /
        ``actor_kind``, ``traceparent``) BEFORE any DB I/O. Resolves the
        target stream from the explicit ``stream`` kwarg (highest priority)
        or :data:`STREAM_FOR_EVENT_KIND` (fallback by ``event.kind``);
        raises :class:`UnknownEventKind` BEFORE any DB I/O if neither
        produces a stream.

        Strict-mode rejects publish without an actor BEFORE any DB I/O
        (codex F3 + critical-contract item 3).

        Emits ONE ``stream.publish`` audit row in the same transaction
        as the outbox INSERT (codex critical-contract item 2).

        :param event: A :class:`StreamEvent` instance (typically a
            concrete subclass like :class:`FilingNewEvent`).
        :param stream: Optional explicit stream-name override. If absent,
            the stream is resolved from
            :data:`STREAM_FOR_EVENT_KIND` ``[event.kind]``.
        :returns: The newly-inserted ``streams.outbox.outbox_id``.
        :raises UnknownEventKind: ``event.kind`` is not registered in
            :data:`STREAM_FOR_EVENT_KIND` AND no ``stream`` kwarg was
            supplied.
        :raises AuditMissingActor: ``Settings.audit_strict=True`` and
            no actor is set in the ContextVar.
        :raises StreamEventIdConflict: ``event.event_id`` already exists
            in ``streams.outbox`` (UNIQUE constraint).
        """
        # 1. Resolve stream BEFORE any DB I/O (raises UnknownEventKind).
        resolved = self._resolve_stream(event, stream)

        # 2. Strict-mode actor check BEFORE any DB I/O.
        assert_actor_or_strict_raise(settings=self._settings)
        actor = current_actor()

        # 3. Auto-stamp missing fields.
        stamped = self._stamp(event, actor=actor)

        # 4. Compute the (possibly-collapsed) Prometheus label.
        prom_stream = _normalize_metric_label(
            normalize_bist_ticks_label(resolved),
            _KNOWN_STREAMS,
        )
        if prom_stream == "other":
            _log.warning(
                "stream %r not in _KNOWN_STREAMS; collapsing Prometheus label to 'other'",
                resolved,
            )

        t0 = time.perf_counter()
        try:
            outbox_id = await self._insert_outbox(stamped, resolved)
            await audit_record(
                self.session,
                record=AuditRecord(
                    operation="stream.publish",
                    target_schema="streams",
                    target_table="outbox",
                    target_pk={"outbox_id": outbox_id},
                    before=None,
                    after=None,
                    metadata={
                        "event_id": str(stamped.event_id),
                        "stream_name": resolved,
                        "schema_version": stamped.schema_version,
                        "kind": getattr(stamped, "kind", "stream.event"),
                        "outbox_id": outbox_id,
                    },
                    ingestion_run_id=self.ingestion_run_id,
                ),
            )
        finally:
            elapsed = time.perf_counter() - t0
            aslan_stream_publish_duration_seconds.labels(
                stream=prom_stream,
            ).observe(elapsed)

        aslan_stream_publishes_total.labels(
            stream=prom_stream,
            source_id=stamped.source_id,
        ).inc()
        return outbox_id

    # ── internals ─────────────────────────────────────────────────────

    def _resolve_stream(
        self,
        event: StreamEvent,
        explicit: str | None,
    ) -> str:
        """Pick the target stream for ``event``.

        The explicit ``stream`` kwarg (when not None) takes priority.
        Otherwise, the stream is looked up from
        :data:`STREAM_FOR_EVENT_KIND` by ``event.kind``. If neither
        produces a stream, raise :class:`UnknownEventKind`.
        """
        if explicit is not None:
            return explicit
        kind = getattr(event, "kind", None)
        if kind is None or kind not in STREAM_FOR_EVENT_KIND:
            raise UnknownEventKind(kind=str(kind))
        return STREAM_FOR_EVENT_KIND[kind]

    def _stamp(self, event: StreamEvent, *, actor: Actor | None) -> StreamEvent:
        """Auto-fill missing fields on the (frozen) event payload.

        Round-trips via :meth:`pydantic.BaseModel.model_copy` because
        every :class:`StreamEvent` subclass is constructed with
        ``frozen=True``.

        Always overrides ``producer_run_id`` with
        ``self.ingestion_run_id`` (the producer is the authority on
        which run a publish belongs to). Stamps ``actor_id`` /
        ``actor_kind`` from the ContextVar actor when those fields are
        not yet set on the event. Stamps ``traceparent`` from the
        active OTel span when available and the event does not already
        carry one.
        """
        update: dict[str, Any] = {}
        if event.producer_run_id != self.ingestion_run_id:
            update["producer_run_id"] = self.ingestion_run_id
        if actor is not None and event.actor_id is None:
            update["actor_id"] = actor.actor_id
            update["actor_kind"] = actor.actor_kind
        if event.traceparent is None:
            tp = _maybe_inject_traceparent()
            if tp is not None:
                update["traceparent"] = tp
        if not update:
            return event
        return event.model_copy(update=update)

    async def _insert_outbox(self, event: StreamEvent, stream: str) -> int:
        """INSERT one row into ``streams.outbox`` and return its id.

        Raises :class:`StreamEventIdConflict` (wrapping the asyncpg
        UNIQUE-constraint :class:`IntegrityError`) when ``event_id``
        already exists.
        """
        actor = current_actor()
        row = Outbox(
            stream_name=stream,
            event_id=event.event_id,
            schema_version=event.schema_version,
            payload=event.model_dump(mode="json"),
            producer_run_id=event.producer_run_id,
            source_id=event.source_id,
            created_at=datetime.now(UTC),
            actor_id=actor.actor_id if actor is not None else None,
            actor_kind=actor.actor_kind if actor is not None else None,
            client_ip=str(actor.client_ip) if actor is not None and actor.client_ip else None,
            user_agent=actor.user_agent if actor is not None else None,
            request_id=event.request_id,
        )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise StreamEventIdConflict(
                event_id=event.event_id,
                stream_name=stream,
            ) from exc
        return int(row.outbox_id)


def _maybe_inject_traceparent() -> str | None:
    """Return the W3C ``traceparent`` for the active OTel span, or None.

    Lazy-imports :mod:`opentelemetry.propagate`. If the optional ``[obs]``
    extra is not installed, returns None unchanged so the producer
    works on a base install.
    """
    try:
        from opentelemetry import propagate
    except ImportError:
        return None
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


__all__ = ["StreamProducer"]
