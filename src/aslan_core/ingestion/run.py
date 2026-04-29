from __future__ import annotations

import json
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import Token
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aslan_core.audit import Actor, AuditRecord, current_actor, pop_actor, push_actor
from aslan_core.audit import record as audit_record
from aslan_core.errors import UnknownSource


class IngestionRunHandle:
    """Behavioral handle returned by ingestion_run().

    Counters accumulate in memory and are flushed atomically with the
    status transition on context exit (clean or failed).

    The optional ``conn`` reference is held when the caller wants
    audit events for handle-side mutations (set_metadata,
    increment_rows) to be written under the same connection that
    opens / closes the run row. Without it, the handle has no way to
    persist audit events; mutations remain visible only via in-memory
    counters until close.
    """

    def __init__(
        self,
        run_id: int,
        source_id: str,
        job_name: str,
        started_at: datetime,
        conn: AsyncConnection | None = None,
    ) -> None:
        self.id = run_id
        self.source_id = source_id
        self.job_name = job_name
        self.started_at = started_at
        self.rows = 0
        self.docs = 0
        self.bytes = 0
        self.errors = 0
        self._conn = conn

    async def increment_rows(self, n: int = 1) -> None:
        before = self.rows
        self.rows += n
        await self._emit_handle_audit(
            operation="ingestion_run.increment_rows",
            before={"rows": before},
            after={"rows": self.rows},
            metadata={"delta": n},
        )

    async def increment_docs(self, n: int = 1) -> None:
        self.docs += n

    async def increment_bytes(self, n: int) -> None:
        self.bytes += n

    async def increment_errors(self, n: int = 1) -> None:
        self.errors += n

    def set_metadata(self, d: dict[str, Any]) -> None:
        """Replace the run's metadata dict (flushed to DB on context exit).

        Calling this multiple times replaces the previous value wholesale.
        The final value is written by _close_run as part of the status UPDATE.

        Synchronous-and-cached: the audit event is buffered and emitted
        atomically with the close-run UPDATE in :func:`_close_run`,
        because emitting a handle-side audit event mid-scope is racy
        with rollback (the close path may finalize the run as failed
        even after a successful set_metadata).
        """
        before = getattr(self, "_metadata", None)
        self._metadata: dict[str, Any] = d
        # Buffer for emission inside _close_run.
        self._pending_metadata_audits: list[tuple[dict[str, Any] | None, dict[str, Any]]] = getattr(
            self, "_pending_metadata_audits", []
        )
        self._pending_metadata_audits.append((before, d))

    async def _emit_handle_audit(
        self,
        *,
        operation: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        metadata: dict[str, Any],
    ) -> None:
        """Emit an audit event from a handle-side mutation.

        No-op if the handle has no connection (legacy/test paths that
        construct the handle directly). The audit row binds to the
        run via target_pk={'ingestion_run_id': ...}.
        """
        if self._conn is None:
            return
        await _audit_record_on_conn(
            self._conn,
            ingestion_run_id=self.id,
            operation=operation,
            target_pk={"ingestion_run_id": self.id},
            before=before,
            after=after,
            metadata=metadata,
        )


@asynccontextmanager
async def ingestion_run(
    engine: AsyncEngine,
    *,
    source_id: str,
    job_name: str,
    config_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
    actor: Actor | None = None,
) -> AsyncIterator[IngestionRunHandle]:
    """Open a fresh connection from `engine`, INSERT a `src.ingestion_run`
    row with status='running' (committed immediately so the row is visible
    cross-session), yield a handle, and on exit UPDATE status to
    'succeeded' or 'failed' with counters flushed.

    Raises UnknownSource(source_id) if `src.source` has no row for the id.

    When ``actor`` is provided, it is pushed onto the audit ContextVar
    for the duration of the ``with`` block (and popped on exit). All
    mutations issued under this scope inherit the actor automatically.
    Cron jobs / workers should pass ``actor=Actor(...)`` here so they
    don't have to thread the actor through every call site.
    """
    actor_token: Token[Actor | None] | None = None
    if actor is not None:
        actor_token = push_actor(actor)

    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM src.source WHERE source_id = :sid"),
                {"sid": source_id},
            )
            if not exists:
                raise UnknownSource(source_id)

            ac = _audit_cols_for_run()
            result = (
                await conn.execute(
                    text(
                        "INSERT INTO src.ingestion_run "
                        "  (source_id, job_name, status, config_hash, metadata, "
                        "   actor_id, actor_kind) "
                        "VALUES (:sid, :job, 'running', :ch, COALESCE(:md, '{}')::jsonb, "
                        "        :actor_id, :actor_kind) "
                        "RETURNING ingestion_run_id, started_at"
                    ),
                    {
                        "sid": source_id,
                        "job": job_name,
                        "ch": config_hash,
                        "md": _jsonb(metadata),
                        **ac,
                    },
                )
            ).one()
            run_id = result.ingestion_run_id
            # Emit ingestion_run.start before the commit so the audit
            # event is in the same transaction as the row INSERT.
            await _audit_record_on_conn(
                conn,
                ingestion_run_id=run_id,
                operation="ingestion_run.start",
                target_pk={"ingestion_run_id": run_id},
                before=None,
                after={
                    "ingestion_run_id": run_id,
                    "source_id": source_id,
                    "job_name": job_name,
                    "config_hash": config_hash,
                    "metadata": metadata or {},
                    "status": "running",
                },
                metadata={},
            )
            await conn.commit()

            handle = IngestionRunHandle(
                run_id=run_id,
                source_id=source_id,
                job_name=job_name,
                started_at=result.started_at,
                conn=conn,
            )

            try:
                yield handle
            except BaseException as e:
                await _close_run(conn, handle, status="failed", error=_truncate_tb(e))
                raise
            else:
                await _close_run(conn, handle, status="succeeded", error=None)
    finally:
        if actor_token is not None:
            pop_actor(actor_token)


async def _close_run(
    conn: AsyncConnection,
    handle: IngestionRunHandle,
    *,
    status: str,
    error: str | None,
) -> None:
    md = getattr(handle, "_metadata", None)
    params: dict[str, Any] = {
        "status": status,
        "rows": handle.rows,
        "docs": handle.docs,
        "bytes": handle.bytes,
        "errors": handle.errors,
        "err": error,
        "id": handle.id,
    }
    if md is not None:
        # Include metadata column only when the caller set it, so we don't
        # accidentally overwrite an existing value with NULL.
        params["md"] = _jsonb(md)
        sql = (
            "UPDATE src.ingestion_run "
            "   SET status = :status, "
            "       finished_at = now(), "
            "       rows_written = :rows, "
            "       docs_written = :docs, "
            "       bytes_written = :bytes, "
            "       error_count = :errors, "
            "       error = :err, "
            "       metadata = :md "
            " WHERE ingestion_run_id = :id"
        )
    else:
        sql = (
            "UPDATE src.ingestion_run "
            "   SET status = :status, "
            "       finished_at = now(), "
            "       rows_written = :rows, "
            "       docs_written = :docs, "
            "       bytes_written = :bytes, "
            "       error_count = :errors, "
            "       error = :err "
            " WHERE ingestion_run_id = :id"
        )
    await conn.execute(text(sql), params)

    # Flush buffered metadata audits.
    pending = getattr(handle, "_pending_metadata_audits", [])
    for before, after in pending:
        await _audit_record_on_conn(
            conn,
            ingestion_run_id=handle.id,
            operation="ingestion_run.set_metadata",
            target_pk={"ingestion_run_id": handle.id},
            before={"metadata": before} if before is not None else None,
            after={"metadata": after},
            metadata={},
        )

    # Emit the close event so the run lifecycle is fully audited.
    await _audit_record_on_conn(
        conn,
        ingestion_run_id=handle.id,
        operation="ingestion_run.complete",
        target_pk={"ingestion_run_id": handle.id},
        before=None,
        after={
            "ingestion_run_id": handle.id,
            "status": status,
            "rows_written": handle.rows,
            "docs_written": handle.docs,
            "bytes_written": handle.bytes,
            "error_count": handle.errors,
        },
        metadata={"error_truncated": error is not None},
    )
    await conn.commit()


def _truncate_tb(exc: BaseException, limit: int = 4000) -> str:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return tb if len(tb) <= limit else tb[-limit:]


def _jsonb(d: dict[str, Any] | None) -> str | None:
    if d is None:
        return None
    return json.dumps(d)


def _audit_cols_for_run() -> dict[str, Any]:
    """Build the actor_id + actor_kind bind params for the
    src.ingestion_run row's denormalised audit cols.

    Per migration 0009, src.ingestion_run only carries (actor_id,
    actor_kind) — per-call fields like client_ip / user_agent /
    request_id don't apply to a per-run row (a run is not a request).
    """
    a = current_actor()
    if a is None:
        return {"actor_id": None, "actor_kind": None}
    return {"actor_id": a.actor_id, "actor_kind": a.actor_kind}


async def _audit_record_on_conn(
    conn: AsyncConnection,
    *,
    ingestion_run_id: int,
    operation: str,
    target_pk: dict[str, Any],
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    metadata: dict[str, Any],
    target_schema: str = "src",
    target_table: str = "ingestion_run",
) -> None:
    """Bridge from :func:`audit.record` (which accepts AsyncSession) to
    the AsyncConnection used by ``ingestion_run``.

    The recorder's session.execute() call works on AsyncConnection at
    runtime (both expose the same `.execute()` shape), but the type
    annotation forbids it. Re-use ``audit.record``'s logic by passing
    the conn through with a type-check ignore — the recorder reads
    ContextVar + applies strict-mode guard, which is the part we want
    centralised.
    """
    await audit_record(
        conn,  # type: ignore[arg-type]
        record=AuditRecord(
            operation=operation,
            target_schema=target_schema,
            target_table=target_table,
            target_pk=target_pk,
            before=before,
            after=after,
            metadata=metadata,
            ingestion_run_id=ingestion_run_id,
        ),
    )
