"""Codex F18 + F19 + F20 + F21 + F22 deadletter routing regressions.

These tests pin behaviour that the spec / plan called out as
load-bearing during adversarial review:

* **F18** — ON CONFLICT idempotency on the durable
  ``streams.deadletter_log`` row. The very first INSERT for a given
  ``(stream_name, group_name, original_message_id)`` returns
  ``is_first_routing=True``; subsequent INSERTs return False with the
  existing ``failure_id`` and ``redis_message_id``.
* **F19** — paginated XRANGE walk in
  :func:`find_orphan_in_deadletter_stream` MUST page beyond ``count``
  per call; orphans past the first page must still be discovered.
* **F20** — fresh other-owner intent (heartbeat <60 s old) → adopter
  aborts; no XADD; ``AdoptionResult.action == "abort"``.
* **F21** — stale adoption that finds the prior owner's orphan in
  Redis: index INSERT + log UPDATE + intent DELETE + emit
  ``stream.deadletter_orphan_reconciled``;
  ``AdoptionResult.action == "reconciled_by_us"``.
* **F22** — re-entry attempt cap on ``acquire_or_adopt_intent``: if the
  FOR UPDATE SELECT keeps returning zero rows because concurrent
  adopters reconcile + DELETE the intent before our lock acquires,
  raise :class:`StreamRoutingContention` after >3 attempts.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.errors import StreamRoutingContention
from aslan_core.streams.deadletter import (
    acquire_or_adopt_intent,
    find_orphan_in_deadletter_stream,
    route_to_deadletter,
)

pytestmark = pytest.mark.integration


STREAM = "aslan.kap.filings.new"
GROUP = "internal-test"
DEADLETTER_STREAM = f"{STREAM}.deadletter"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _wipe_deadletter_state(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    yield
    async with session_factory() as session:
        await session.execute(text("DELETE FROM streams.deadletter_xadd_intent"))
        await session.execute(text("DELETE FROM streams.deadletter_redis_index"))
        await session.execute(text("DELETE FROM streams.deadletter_log"))
        await session.commit()
    # Trim the dead-letter stream so subsequent tests start with a
    # clean upper bound (otherwise F19 can leak state across runs).
    with contextlib.suppress(Exception):
        await redis_client.delete(DEADLETTER_STREAM)


# ── F18 — ON CONFLICT DO UPDATE returns existing failure_id ───────────


async def test_routing_step_1_returns_existing_failure_id_on_conflict(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F18 — pre-insert a deadletter_log row; second INSERT of the
    same ``(stream, group, original_message_id)`` triple via
    :func:`route_to_deadletter` reuses the existing ``failure_id`` and
    ``redis_message_id`` instead of failing the unique constraint.

    The contract: the INSERT in step 1 of the routing protocol uses
    ``ON CONFLICT (stream, group, original_message_id) DO UPDATE`` so
    ``RETURNING xmax = 0 AS is_first_routing`` returns False on the
    second call. The protocol then short-circuits to the existing
    ``redis_message_id`` (codex F18 round 8)."""
    eid = "00000000-0000-0000-0000-000000000018"
    mid = "18-0"

    # Pre-seed the row as if a prior call had completed routing.
    await session.execute(
        text(
            """
            INSERT INTO streams.deadletter_log (
                stream_name, group_name, original_message_id, deadletter_stream,
                event_id, consumer_name, failure_count, last_error,
                payload_excerpt, redis_message_id, routed_at_redis
            ) VALUES (
                :stream, :group, :mid, :dl, CAST(:eid AS UUID),
                'consumer-prior', 5, 'prior boom', '{}'::jsonb,
                'pre-seeded-rid', now()
            )
            """
        ),
        {
            "stream": STREAM,
            "group": GROUP,
            "mid": mid,
            "dl": DEADLETTER_STREAM,
            "eid": eid,
        },
    )
    pre_failure_id = (
        await session.execute(
            text(
                "SELECT failure_id FROM streams.deadletter_log "
                "WHERE stream_name=:s AND group_name=:g AND original_message_id=:m"
            ),
            {"s": STREAM, "g": GROUP, "m": mid},
        )
    ).scalar_one()
    await session.commit()

    # Second routing call for the SAME (stream, group, mid). It must
    # short-circuit to the existing redis_message_id without doing a
    # second XADD (codex F18 contract).
    xlen_before = await redis_client.xlen(DEADLETTER_STREAM)
    result = await route_to_deadletter(
        redis=redis_client,
        session_factory=session_factory,
        stream=STREAM,
        group=GROUP,
        consumer_name="consumer-second",
        message_id=mid,
        event_id=__import__("uuid").UUID(eid),
        failure_count=99,  # would have escalated, but row already exists
        last_error="second attempt boom",
        payload_excerpt="{}",
        owner_id="owner:second",
    )

    # Redis stream length unchanged — no second XADD.
    assert await redis_client.xlen(DEADLETTER_STREAM) == xlen_before
    # The result reuses the existing redis_message_id.
    from aslan_core.streams.deadletter import RoutingComplete

    assert isinstance(result, RoutingComplete)
    assert result.redis_message_id == "pre-seeded-rid"

    # The same failure_id row remains (no duplicate).
    failure_ids = (
        (
            await session.execute(
                text(
                    "SELECT failure_id FROM streams.deadletter_log "
                    "WHERE stream_name=:s AND group_name=:g AND original_message_id=:m"
                ),
                {"s": STREAM, "g": GROUP, "m": mid},
            )
        )
        .scalars()
        .all()
    )
    assert failure_ids == [pre_failure_id]


# ── F19 — paginated XRANGE walk finds orphan beyond first page ──────


async def test_janitor_pagination_finds_orphan_beyond_first_page(
    redis_client: Redis,
) -> None:
    """F19 — the dead-letter stream walk in
    :func:`find_orphan_in_deadletter_stream` defaults to ``count=10000``
    per page. We seed >page_size entries between the lower_bound and
    the orphan we're searching for; the walk MUST page through and
    eventually return the orphan's redis_message_id rather than
    classifying it as ``None`` (which would mark the prior owner's
    XADD as "lost" and trigger a duplicate redrive)."""
    # Use a small page_size so we don't have to seed 10k entries.
    page_size = 50
    target_failure_id = 19_99919

    # Capture the lower_bound BEFORE we add any entries.
    lower_bound = "0-0"  # empty stream
    # Seed some noise BEFORE the target.
    for i in range(page_size + 5):
        await redis_client.xadd(
            DEADLETTER_STREAM,
            {
                "event_id": f"00000000-0000-0000-0000-{i:012d}",
                "failure_id": str(100 + i),  # NOT the target
                "stream_name": STREAM,
            },
        )
    # Then the target.
    target_msg_id = await redis_client.xadd(
        DEADLETTER_STREAM,
        {
            "event_id": "00000000-0000-0000-0000-000000019999",
            "failure_id": str(target_failure_id),
            "stream_name": STREAM,
        },
    )
    # Then more noise (so the upper-bound capture lands past the target).
    for i in range(20):
        await redis_client.xadd(
            DEADLETTER_STREAM,
            {
                "event_id": f"00000000-0000-0000-0000-{(2000 + i):012d}",
                "failure_id": str(200 + i),
                "stream_name": STREAM,
            },
        )

    found = await find_orphan_in_deadletter_stream(
        redis_client,
        STREAM,
        lower_bound=lower_bound,
        target_failure_id=target_failure_id,
        page_size=page_size,
    )
    assert found is not None, (
        f"orphan with failure_id={target_failure_id} (XADD={target_msg_id}) "
        f"must be discovered across paginated XRANGE walks; got None"
    )
    target_msg_id_str = (
        target_msg_id.decode() if isinstance(target_msg_id, bytes) else str(target_msg_id)
    )
    assert found == target_msg_id_str


# ── F20 — fresh other-owner intent → abort ───────────────────────────


async def test_step_2_5_ownership_gate_aborts_when_fresh_intent_held_by_other_worker(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F20 — Worker A holds a fresh intent (``owner_heartbeat_at`` <60 s
    old). Worker B calls :func:`acquire_or_adopt_intent`. The conflict
    branch sees a fresh other-owner heartbeat and returns
    ``AdoptionResult.action == "abort"``. B does NOT XADD, so the
    Redis stream length is unchanged."""
    fid = await _insert_deadletter_log(session, eid="00000000-0000-0000-0000-000000000020")

    # Pre-insert worker A's intent with a fresh heartbeat (just now).
    await session.execute(
        text(
            """
            INSERT INTO streams.deadletter_xadd_intent (
                failure_id, stream_name, intent_at, redis_lower_bound_id,
                owner_id, owner_heartbeat_at
            ) VALUES (
                :fid, :stream, now(), '0-0', 'consumer:A', now()
            )
            """
        ),
        {"fid": fid, "stream": STREAM},
    )
    await session.commit()

    xlen_before = await redis_client.xlen(DEADLETTER_STREAM)

    async with session_factory() as s:
        result = await acquire_or_adopt_intent(
            s,
            redis_client,
            fid=fid,
            my_owner_id="consumer:B",
            my_lower_bound="0-0",
            stream=STREAM,
        )
        await s.rollback()

    assert result.action == "abort"
    assert result.lower_bound is None
    assert await redis_client.xlen(DEADLETTER_STREAM) == xlen_before


# ── F21 — stale adoption reconciles prior owner's orphan ────────────


async def test_stale_adoption_reconciles_prior_orphan_before_overwriting(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F21 — A's intent is stale (heartbeat >60s old). A's XADD landed
    BUT the index INSERT didn't (the worker crashed between). B calls
    :func:`acquire_or_adopt_intent`. The procedure:

        - SELECT FOR UPDATE finds A's stale row.
        - Calls :func:`find_orphan_in_deadletter_stream` from A's
          ``redis_lower_bound_id``; finds A's XADD entry.
        - INSERT into ``streams.deadletter_redis_index``.
        - UPDATE ``streams.deadletter_log`` with the orphan's rid.
        - DELETE the stale intent.
        - Emit ``stream.deadletter_orphan_reconciled`` audit row.

    Returns ``AdoptionResult(action="reconciled_by_us", redis_message_id=<A's
    orphan rid>)``."""
    eid = "00000000-0000-0000-0000-000000000021"
    fid = await _insert_deadletter_log(session, eid=eid)

    # Capture lower_bound BEFORE A's XADD (empty stream → "0-0").
    a_lower_bound = "0-0"
    # Simulate A's XADD landing but the index INSERT not landing.
    a_orphan_msg_id = await redis_client.xadd(
        DEADLETTER_STREAM,
        {
            "event_id": eid,
            "failure_id": str(fid),
            "stream_name": STREAM,
        },
    )
    a_orphan_msg_id_str = (
        a_orphan_msg_id.decode() if isinstance(a_orphan_msg_id, bytes) else str(a_orphan_msg_id)
    )

    # Insert A's stale intent (heartbeat 2 min old).
    await session.execute(
        text(
            """
            INSERT INTO streams.deadletter_xadd_intent (
                failure_id, stream_name, intent_at, redis_lower_bound_id,
                owner_id, owner_heartbeat_at
            ) VALUES (
                :fid, :stream, now() - INTERVAL '5 minutes', :lb,
                'consumer:A-crashed', now() - INTERVAL '2 minutes'
            )
            """
        ),
        {"fid": fid, "stream": STREAM, "lb": a_lower_bound},
    )
    await session.commit()

    async with session_factory() as s:
        result = await acquire_or_adopt_intent(
            s,
            redis_client,
            fid=fid,
            my_owner_id="consumer:B",
            my_lower_bound=await _capture_lower_bound(redis_client),
            stream=STREAM,
        )
        await s.commit()

    assert result.action == "reconciled_by_us"
    assert result.redis_message_id == a_orphan_msg_id_str

    # log row updated.
    log_row = (
        await session.execute(
            text(
                "SELECT redis_message_id, routed_at_redis FROM streams.deadletter_log "
                "WHERE failure_id = :fid"
            ),
            {"fid": fid},
        )
    ).one()
    assert log_row.redis_message_id == a_orphan_msg_id_str
    assert log_row.routed_at_redis is not None

    # index row inserted.
    idx_rid = (
        await session.execute(
            text(
                "SELECT redis_message_id FROM streams.deadletter_redis_index "
                "WHERE failure_id = :fid"
            ),
            {"fid": fid},
        )
    ).scalar_one()
    assert idx_rid == a_orphan_msg_id_str

    # intent DELETEd.
    intent_count = (
        await session.execute(
            text("SELECT count(*) FROM streams.deadletter_xadd_intent WHERE failure_id = :fid"),
            {"fid": fid},
        )
    ).scalar_one()
    assert intent_count == 0

    # stream.deadletter_orphan_reconciled audit emitted.
    audit_n = (
        await session.execute(
            text(
                """
                SELECT count(*)
                FROM audit.events
                WHERE operation = 'stream.deadletter_orphan_reconciled'
                  AND target_pk @> jsonb_build_object(
                      'failure_id', CAST(:fid AS BIGINT)
                  )
                """
            ),
            {"fid": fid},
        )
    ).scalar_one()
    assert audit_n == 1


# ── F22 — attempt cap raises StreamRoutingContention ─────────────────


async def test_acquire_or_adopt_intent_attempt_cap_raises_routing_contention(
    session: AsyncSession,
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """F22 — :func:`acquire_or_adopt_intent` re-enters from the top
    when the FOR UPDATE SELECT returns zero rows (a concurrent adopter
    reconciled + DELETEd the intent between our INSERT...DO NOTHING
    and our SELECT). The bounded depth ``_attempt`` caps re-entry at
    3; on the 4th retry the procedure raises
    :class:`StreamRoutingContention`.

    We exercise the cap by patching ``session.execute`` so the INSERT
    keeps returning ``None`` (ON CONFLICT) and the SELECT keeps
    returning ``None`` (deleted by phantom adopter). The procedure
    self-recurses 3 times then raises."""
    fid = await _insert_deadletter_log(
        session,
        eid="00000000-0000-0000-0000-000000000022",
    )

    # Patch acquire_or_adopt_intent's internals via a wrapping session
    # that always returns "no row" from the SELECT and ON CONFLICT
    # from the INSERT.
    from unittest.mock import MagicMock

    class _ResultProxy:
        def first(self) -> None:
            return None

        def one_or_none(self) -> None:
            return None

        def scalar_one_or_none(self) -> None:
            return None

        def scalar_one(self) -> int:
            raise AssertionError("scalar_one should not be called in this test")

    class _AlwaysNothingSession:
        """Stand-in for AsyncSession that mimics the contention loop.

        Every INSERT returns 'ON CONFLICT' (no rows from RETURNING).
        Every FOR UPDATE SELECT returns no rows (phantom DELETE).
        The procedure must re-enter from the top 3 times then raise.
        """

        async def execute(self, *_args: Any, **_kwargs: Any) -> _ResultProxy:
            return _ResultProxy()

        async def commit(self) -> None:
            pass

        async def rollback(self) -> None:
            pass

    fake_session = _AlwaysNothingSession()

    # The real Redis client is fine; the Redis-side helper is only
    # called from the stale-adoption branch, which we never reach
    # because the SELECT returns None at every attempt.
    redis_stub = MagicMock()

    with pytest.raises(StreamRoutingContention) as exc_info:
        await acquire_or_adopt_intent(
            fake_session,  # type: ignore[arg-type]
            redis_stub,
            fid=fid,
            my_owner_id="consumer:storm",
            my_lower_bound="0-0",
            stream=STREAM,
        )
    assert exc_info.value.failure_id == fid
    assert exc_info.value.attempts > 3


# ── helpers ─────────────────────────────────────────────────────────


async def _insert_deadletter_log(session: AsyncSession, *, eid: str) -> int:
    """Insert a minimal ``streams.deadletter_log`` row and return its
    ``failure_id``. Mirrors the helper in
    ``test_stream_deadletter_janitor.py`` but pinned to this test
    module's STREAM/GROUP constants."""
    fid: int = (
        await session.execute(
            text(
                """
                INSERT INTO streams.deadletter_log (
                    stream_name, deadletter_stream, event_id,
                    original_message_id, group_name, consumer_name,
                    failure_count, last_error, payload_excerpt
                ) VALUES (
                    :stream, :dl, CAST(:eid AS UUID),
                    :mid, :group, 'consumer:test',
                    5, 'boom', '{}'::jsonb
                )
                RETURNING failure_id
                """
            ),
            {
                "stream": STREAM,
                "dl": DEADLETTER_STREAM,
                "eid": eid,
                "mid": eid[-4:] + "-0",
                "group": GROUP,
            },
        )
    ).scalar_one()
    await session.commit()
    return fid


async def _capture_lower_bound(redis: Redis) -> str:
    info = await redis.xinfo_stream(DEADLETTER_STREAM)
    last = info.get("last-entry") if info else None
    if not last:
        return "0-0"
    rid = last[0]
    return rid.decode() if isinstance(rid, bytes) else str(rid)
