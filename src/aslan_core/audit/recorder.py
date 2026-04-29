"""Audit recorder — single gateway for INSERTs into ``audit.events``.

Every public mutation in aslan-core calls :func:`record` exactly once
(per spec §4 / Task 5 of the v0.3.0 plan). The function runs inside
the caller's session/transaction so the audit row is atomic with the
mutation it audits — if the caller rolls back, the audit row goes too.

Strict-mode boundary (codex F3, 2026-04-29):

  * ``Settings.audit_strict=True`` — mutations without an actor raise
    :class:`AuditMissingActor` *before* writing to the DB. No
    ``system:unknown`` rows ever land in the audit log.
  * ``Settings.audit_strict=False`` (default during the v0.3 → v1.0
    migration window) — a structured WARNING is logged and the audit
    row is written with ``actor_id='system:unknown'``,
    ``actor_kind='system'``.

The actor (and request context) is read from the ContextVar inside
``record()`` rather than passed as a parameter — that prevents a
mutation from spoofing identity by passing a hand-built Actor.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.audit.context import current_actor
from aslan_core.config import Settings
from aslan_core.errors import AuditMissingActor

_log = logging.getLogger(__name__)

_SYSTEM_UNKNOWN_ACTOR_ID = "system:unknown"
_SYSTEM_UNKNOWN_ACTOR_KIND = "system"


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One row to be written to ``audit.events``.

    Constructed and passed to :func:`record` by every public mutation.
    The actor (and request context) is *not* a field here — it is read
    from the ContextVar at insert time so a mutation cannot spoof
    identity.
    """

    operation: str
    target_schema: str
    target_table: str
    target_pk: dict[str, Any]
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    metadata: dict[str, Any] = field(default_factory=dict)
    ingestion_run_id: int | None = None


_INSERT_SQL = text(
    "INSERT INTO audit.events ("
    "  actor_id, actor_kind, client_ip, user_agent, request_id, "
    "  ingestion_run_id, operation, target_schema, target_table, "
    "  target_pk, before, after, metadata"
    ") VALUES ("
    "  :actor_id, :actor_kind, :client_ip, :user_agent, :request_id, "
    "  :ingestion_run_id, :operation, :target_schema, :target_table, "
    "  CAST(:target_pk AS JSONB), CAST(:before AS JSONB), "
    "  CAST(:after AS JSONB), CAST(:metadata AS JSONB)"
    ")"
)


def assert_actor_or_strict_raise(*, settings: Settings | None = None) -> None:
    """Strict-mode early-check helper called at the top of every public
    mutation method.

    If no actor is set in the ContextVar AND ``Settings.audit_strict``
    is True, raises :class:`AuditMissingActor` BEFORE any mutation I/O.
    In lenient mode, returns silently — :func:`record` will log the
    warning and write ``system:unknown`` later.

    The procurement-grade contract for v0.3.0 customer-facing service
    profiles depends on this raising before any DB INSERT or blob
    upload — without it, strict mode would only catch missing actors
    AFTER side effects ran (codex F3, 2026-04-29).
    """
    settings = settings if settings is not None else Settings()
    if not settings.audit_strict:
        return
    if current_actor() is None:
        raise AuditMissingActor(
            "strict mode: mutation invoked with no actor set in ContextVar; "
            "either call aslan_core.audit.set_actor(...) at the request/job "
            "boundary, pass actor= to ingestion_run(...), or run with "
            "audit_strict=False (non-strict mode is the v0.3 default but is "
            "scheduled for removal in v1.1)"
        )


async def record(
    session: AsyncSession,
    *,
    record: AuditRecord,
    settings: Settings | None = None,
) -> None:
    """Insert one row into ``audit.events`` from the current actor.

    Runs inside the caller's transaction — if the caller rolls back,
    the audit row goes too. Atomicity is the whole point: the row's
    audit columns reflect the actor at write time, and the audit-log
    row reflects the same write; both must commit or roll back together.

    If no actor is set in the ContextVar:

      * ``Settings.audit_strict=True`` — raise :class:`AuditMissingActor`
        BEFORE issuing any SQL.
      * ``Settings.audit_strict=False`` — log a structured WARNING with
        the message tag ``audit_missing_actor`` and write the row with
        ``actor_id='system:unknown'``, ``actor_kind='system'``.

    :param session: AsyncSession from the caller's transaction.
    :param record: The :class:`AuditRecord` to insert.
    :param settings: Optional pre-loaded Settings. Default re-reads
        from environment so the strict-mode flag stays live for tests
        that flip it via env vars.
    """
    settings = settings if settings is not None else Settings()
    actor = current_actor()

    if actor is None:
        if settings.audit_strict:
            raise AuditMissingActor(
                f"audit.record(operation={record.operation!r}) called with "
                "no actor set; either set actor before mutation, or run "
                "with audit_strict=False"
            )
        _log.warning(
            "audit_missing_actor: operation=%s target=%s.%s",
            record.operation,
            record.target_schema,
            record.target_table,
        )
        actor_id: str = _SYSTEM_UNKNOWN_ACTOR_ID
        actor_kind: str = _SYSTEM_UNKNOWN_ACTOR_KIND
        client_ip: str | None = None
        user_agent: str | None = None
        request_id = None
    else:
        actor_id = actor.actor_id
        actor_kind = actor.actor_kind
        client_ip = actor.client_ip
        user_agent = actor.user_agent
        request_id = actor.request_id

    await session.execute(
        _INSERT_SQL,
        {
            "actor_id": actor_id,
            "actor_kind": actor_kind,
            "client_ip": client_ip,
            "user_agent": user_agent,
            "request_id": request_id,
            "ingestion_run_id": record.ingestion_run_id,
            "operation": record.operation,
            "target_schema": record.target_schema,
            "target_table": record.target_table,
            "target_pk": json.dumps(record.target_pk, default=str),
            "before": (
                json.dumps(record.before, default=str) if record.before is not None else None
            ),
            "after": (json.dumps(record.after, default=str) if record.after is not None else None),
            "metadata": json.dumps(record.metadata, default=str),
        },
    )

    # Prometheus: count audit events by (operation, actor_kind). The
    # metric is a no-op when prometheus_client is not installed (the
    # base venv without [obs] extra) — see metrics module docstring.
    #
    # ``operation`` is normalized against the closed allow-list to
    # bound metric cardinality (codex Batch 4) — defensive for the
    # case where a future emitter adds a new operation string but
    # forgets to update _KNOWN_AUDIT_OPERATIONS. ``actor_kind`` is
    # already CHECK-constrained in audit.events to {user, service,
    # system}, so no normalization is needed there.
    from aslan_core.observability import metrics

    metrics.audit_events.labels(
        operation=metrics._normalize_metric_label(
            record.operation, metrics._KNOWN_AUDIT_OPERATIONS
        ),
        actor_kind=actor_kind,
    ).inc()
