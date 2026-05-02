"""Heartbeat maintenance loop for owner-held intents.

Codex F20 round 9 + spec §7. A worker that holds an active intent
(between step 2.5's ownership-gate INSERT and step 3.5's intent
DELETE) MUST refresh ``owner_heartbeat_at`` every 30 seconds so a slow
but live routing attempt is not falsely adopted by a sibling worker.

Public surface:

* :func:`heartbeat_active_intents` — long-running task that runs a
  single ``UPDATE … WHERE owner_id = :owner_id`` per cycle. Multiple
  in-flight routes from the same worker share one heartbeat tick.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = logging.getLogger(__name__)


async def heartbeat_active_intents(
    session_factory: async_sessionmaker[AsyncSession],
    owner_id: str,
    *,
    interval_s: float = 30.0,
) -> None:
    """Refresh ``owner_heartbeat_at`` for every intent row owned by
    ``owner_id``, every ``interval_s`` seconds.

    The 30s default is half the 60s adoption window — a single missed
    heartbeat (long Postgres query, GC pause, brief process suspend)
    does not trigger spurious adoption. Two missed heartbeats in a
    row (>=60s) DOES trigger adoption, which is the correct signal
    that the worker is stuck or crashed.

    Heartbeat failures are non-fatal: the next iteration retries.

    :param session_factory: AsyncSession factory; the loop opens a
        fresh session per iteration so a transient DB error doesn't
        poison subsequent ones.
    :param owner_id: The per-process worker identifier this loop
        refreshes.
    :param interval_s: Seconds between iterations. Default 30s.
    """
    while True:
        try:
            async with session_factory() as session:
                await session.execute(
                    text(
                        "UPDATE streams.deadletter_xadd_intent "
                        "SET owner_heartbeat_at = now() "
                        "WHERE owner_id = :owner_id"
                    ),
                    {"owner_id": owner_id},
                )
                await session.commit()
        except SQLAlchemyError:
            _log.exception("heartbeat update failed; will retry")
        await asyncio.sleep(interval_s)


__all__ = ["heartbeat_active_intents"]
