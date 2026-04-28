from __future__ import annotations

import json
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aslan_core.errors import UnknownSource


class IngestionRunHandle:
    """Behavioral handle returned by ingestion_run().

    Counters accumulate in memory and are flushed atomically with the
    status transition on context exit (clean or failed).
    """

    def __init__(
        self,
        run_id: int,
        source_id: str,
        job_name: str,
        started_at: datetime,
    ) -> None:
        self.id = run_id
        self.source_id = source_id
        self.job_name = job_name
        self.started_at = started_at
        self.rows = 0
        self.docs = 0
        self.bytes = 0
        self.errors = 0

    async def increment_rows(self, n: int = 1) -> None:
        self.rows += n

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
        """
        self._metadata: dict[str, Any] = d


@asynccontextmanager
async def ingestion_run(
    engine: AsyncEngine,
    *,
    source_id: str,
    job_name: str,
    config_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AsyncIterator[IngestionRunHandle]:
    """Open a fresh connection from `engine`, INSERT a `src.ingestion_run`
    row with status='running' (committed immediately so the row is visible
    cross-session), yield a handle, and on exit UPDATE status to
    'succeeded' or 'failed' with counters flushed.

    Raises UnknownSource(source_id) if `src.source` has no row for the id.
    """
    async with engine.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM src.source WHERE source_id = :sid"),
            {"sid": source_id},
        )
        if not exists:
            raise UnknownSource(source_id)

        result = (
            await conn.execute(
                text(
                    "INSERT INTO src.ingestion_run "
                    "  (source_id, job_name, status, config_hash, metadata) "
                    "VALUES (:sid, :job, 'running', :ch, COALESCE(:md, '{}')::jsonb) "
                    "RETURNING ingestion_run_id, started_at"
                ),
                {
                    "sid": source_id,
                    "job": job_name,
                    "ch": config_hash,
                    "md": _jsonb(metadata),
                },
            )
        ).one()
        await conn.commit()

        handle = IngestionRunHandle(
            run_id=result.ingestion_run_id,
            source_id=source_id,
            job_name=job_name,
            started_at=result.started_at,
        )

        try:
            yield handle
        except BaseException as e:
            await _close_run(conn, handle, status="failed", error=_truncate_tb(e))
            raise
        else:
            await _close_run(conn, handle, status="succeeded", error=None)


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
    await conn.commit()


def _truncate_tb(exc: BaseException, limit: int = 4000) -> str:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return tb if len(tb) <= limit else tb[-limit:]


def _jsonb(d: dict[str, Any] | None) -> str | None:
    if d is None:
        return None
    return json.dumps(d)
