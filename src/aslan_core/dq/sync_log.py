"""dq.sync_log — public interface for puller invocation lifecycle.

Usage:

    async with sync_log.run(engine=eng, source="kap", operation="tail") as run:
        run.records_ingested(n)
        run.set_upstream_max(ts)
        ...
        # exception -> status='failed', error_summary captured
        # clean -> 'ok' (or 'partial' if records_failed > 0)

Why `engine` not `session`: sync_log lifecycle rows must survive
the caller's transaction state. If the caller's outer transaction
rolls back due to an exception, we still want the FAILED row to
land — otherwise the audit trail loses every transient failure. So
both START and FINISH open their own short-lived connections via
`engine.begin()`, decoupled from any caller transaction. The
trade-off (vs validation/coverage/event, which take `session=`):
the audit row is visible mid-run before the caller commits its real
work. That's correct for an observability call; observability
should report what happened regardless of business-logic outcome.

Known M0 limitation: a process killed between START and FINISH
(SIGKILL, OOM, container forced-restart) leaves a row stuck at
`status='running'` forever. The `sync_log_status_started` partial
index already covers `'running'` rows, so an `audit-stuck-sync-reaper`
cron in M1 can sweep rows where `started_at < now() - INTERVAL '24 h'`
and `status='running'`, marking them `'failed'` with
`error_summary='reaped: process killed between start and finish'`.
This is a forward-deferral, not a bug; the row's existence is the
audit signal that an interrupted run happened.

PII redaction: per spec §12, `error_summary` is run through
`_redact()` before insert, which strips Turkish national ID number
(TC kimlik no, 11 digits) patterns. Tracebacks may incidentally
quote raw KAP filing snippets containing customer IDs; the redact
pass replaces them with `[REDACTED-TC-KIMLIK]` before persistence.
A hardening assert post-redaction prevents leaks.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Self

from sqlalchemy.ext.asyncio import AsyncEngine

from aslan_core.dq._sql import (
    INSERT_SYNC_LOG_START,
    UPDATE_SYNC_LOG_FINISH,
)
from aslan_core.dq.types import SyncRunStatus

_HOST = socket.gethostname()


_TC_KIMLIK_RE = re.compile(r"\b\d{11}\b")


def _redact(text: str) -> str:
    """Redact Turkish national ID numbers (TC kimlik no = 11 digits).

    Per spec §12 PII handling: error_summary may incidentally contain
    raw KAP filing snippets if an exception bubbled up with context.
    Strip anything matching the TC-kimlik regex `\\b\\d{11}\\b`.
    """
    return _TC_KIMLIK_RE.sub("[REDACTED-TC-KIMLIK]", text)


def _parse_iso(iso: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC string into a tz-aware datetime.

    asyncpg refuses string inputs for TIMESTAMPTZ columns; we accept
    strings on the public surface (caller convenience) and convert
    here. ``Z`` suffix is normalized to ``+00:00`` for ``fromisoformat``.
    """
    if iso is None:
        return None
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso)


def _git_sha() -> str:
    """Return the current git SHA, or 'unknown' if git is unavailable.

    Cached per-process — git invocation is expensive and the SHA is
    immutable for the life of the process. Environment-variable
    override (`ASLAN_GIT_SHA`) is checked first so CI and container
    builds never spawn `git` (the binary may not even be present
    inside the runtime image).

    Concurrent first-callers may both invoke subprocess.run before the
    cache is populated; harmless duplicate work — the result is
    deterministic and the assignment is atomic under the GIL.
    """
    cached = getattr(_git_sha, "_cached", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    env_sha = os.environ.get("ASLAN_GIT_SHA")
    if env_sha:
        _git_sha._cached = env_sha  # type: ignore[attr-defined]
        return env_sha
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 — git is expected on PATH; failures fall through to "unknown"
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        sha = out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:  # observability helper; any failure means "unknown"
        sha = "unknown"
    _git_sha._cached = sha  # type: ignore[attr-defined]
    return sha


class SyncRun:
    """Mutable handle yielded by `sync_log.run()`.

    Public methods set fields on the in-flight row; the context
    manager finalizes the row on exit. Calling these after exit is
    a programming error and raises RuntimeError.
    """

    __slots__ = (
        "_db_max_at",
        "_finalized",
        "_records_failed",
        "_records_ingested",
        "_sync_id",
        "_upstream_max_at",
    )

    def __init__(self, sync_id: int) -> None:
        self._sync_id: int = sync_id
        self._records_ingested: int | None = None
        self._records_failed: int | None = None
        self._upstream_max_at: str | None = None
        self._db_max_at: str | None = None
        self._finalized: bool = False

    @property
    def sync_id(self) -> int:
        return self._sync_id

    def records_ingested(self, n: int) -> Self:
        if self._finalized:
            raise RuntimeError("SyncRun already finalized")
        self._records_ingested = n
        return self

    def records_failed(self, n: int) -> Self:
        if self._finalized:
            raise RuntimeError("SyncRun already finalized")
        self._records_failed = n
        return self

    def set_upstream_max(self, iso_utc: str) -> Self:
        if self._finalized:
            raise RuntimeError("SyncRun already finalized")
        self._upstream_max_at = iso_utc
        return self

    def set_db_max(self, iso_utc: str) -> Self:
        if self._finalized:
            raise RuntimeError("SyncRun already finalized")
        self._db_max_at = iso_utc
        return self


@asynccontextmanager
async def run(
    *,
    engine: AsyncEngine,
    source: str,
    operation: str,
) -> AsyncIterator[SyncRun]:
    """Open a sync-log lifecycle row.

    On enter: INSERT a row with status='running' on a fresh
    connection (commits immediately) and return the SyncRun.
    On normal exit: UPDATE to status='ok' (or 'partial' if
    records_failed > 0). On exception: UPDATE to status='failed'
    with truncated traceback, then re-raise.

    Both INSERT and UPDATE use independent `engine.begin()` blocks,
    so the row is durable regardless of any surrounding caller
    transaction state.

    Times are UTC TIMESTAMPTZ (ISO-8601 strings).
    """
    started_at = datetime.now(UTC)

    async with engine.begin() as conn:
        result = await conn.execute(
            INSERT_SYNC_LOG_START,
            {
                "source": source,
                "operation": operation,
                "started_at": started_at,
                "git_sha": _git_sha(),
                "host": _HOST,
            },
        )
        sync_id = int(result.scalar_one())
    handle = SyncRun(sync_id)

    raised: BaseException | None = None
    try:
        yield handle
    except BaseException as e:
        raised = e
        raise
    finally:
        completed_at = datetime.now(UTC)
        error_summary: str | None
        if raised is not None:
            status = SyncRunStatus.FAILED.value
            error_summary = "".join(traceback.format_exception_only(type(raised), raised))[:4000]
        elif handle._records_failed and handle._records_failed > 0:
            status = SyncRunStatus.PARTIAL.value
            error_summary = None
        else:
            status = SyncRunStatus.OK.value
            error_summary = None

        if error_summary is not None:
            error_summary = _redact(error_summary)[:4000]  # redact, then truncate
            # Hardening assert: the redacted string must not contain a TC kimlik
            # pattern. If this fires, the regex needs widening.
            assert _TC_KIMLIK_RE.search(error_summary) is None, "TC-kimlik leaked through redaction"

        async with engine.begin() as conn:
            await conn.execute(
                UPDATE_SYNC_LOG_FINISH,
                {
                    "sync_id": sync_id,
                    "completed_at": completed_at,
                    "status": status,
                    "records_ingested": handle._records_ingested,
                    "records_failed": handle._records_failed,
                    "upstream_max_at": _parse_iso(handle._upstream_max_at),
                    "db_max_at": _parse_iso(handle._db_max_at),
                    "error_summary": error_summary,
                },
            )
        handle._finalized = True
