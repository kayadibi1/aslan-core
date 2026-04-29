from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.ingestion.watermarks import WatermarkStore

pytestmark = pytest.mark.integration


async def _seed_source(session: AsyncSession, sid: str = "kap") -> None:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (:sid, 'KAP', 'scraper', 'open') ON CONFLICT DO NOTHING"
        ),
        {"sid": sid},
    )
    await session.commit()


async def _wipe_watermarks(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM src.watermark"))
    await session.commit()


async def test_get_returns_none_when_absent(session: AsyncSession) -> None:
    await _seed_source(session)
    await _wipe_watermarks(session)
    store = WatermarkStore(session)
    assert await store.get("kap", "j", "k") is None


async def test_advance_inserts_when_no_row_and_expected_is_none(
    session: AsyncSession,
) -> None:
    await _seed_source(session)
    await _wipe_watermarks(session)
    store = WatermarkStore(session)
    ok = await store.advance("kap", "j", "k", new_cursor="100", expected_cursor=None)
    await session.commit()
    assert ok is True
    assert await store.get("kap", "j", "k") == "100"


async def test_advance_updates_when_expected_matches(session: AsyncSession) -> None:
    await _seed_source(session)
    await _wipe_watermarks(session)
    store = WatermarkStore(session)
    await store.advance("kap", "j", "k", new_cursor="1", expected_cursor=None)
    await session.commit()

    ok = await store.advance("kap", "j", "k", new_cursor="2", expected_cursor="1")
    await session.commit()
    assert ok is True
    assert await store.get("kap", "j", "k") == "2"


async def test_advance_returns_false_on_cas_miss(session: AsyncSession) -> None:
    await _seed_source(session)
    await _wipe_watermarks(session)
    store = WatermarkStore(session)
    await store.advance("kap", "j", "k", new_cursor="1", expected_cursor=None)
    await session.commit()

    ok = await store.advance("kap", "j", "k", new_cursor="3", expected_cursor="2")
    await session.commit()
    assert ok is False
    assert await store.get("kap", "j", "k") == "1"


async def test_set_unconditionally_overwrites(session: AsyncSession) -> None:
    await _seed_source(session)
    await _wipe_watermarks(session)
    store = WatermarkStore(session)
    await store.advance("kap", "j", "k", new_cursor="5", expected_cursor=None)
    await session.commit()
    await store.set("kap", "j", "k", "1")  # regression on purpose
    await session.commit()
    assert await store.get("kap", "j", "k") == "1"


async def test_advance_no_row_and_expected_value_returns_false(
    session: AsyncSession,
) -> None:
    """If you expect 'X' but no row exists, CAS misses (returns False).
    Don't silently insert."""
    await _seed_source(session)
    await _wipe_watermarks(session)
    store = WatermarkStore(session)
    ok = await store.advance("kap", "j", "k", new_cursor="2", expected_cursor="1")
    await session.commit()
    assert ok is False
    assert await store.get("kap", "j", "k") is None


async def test_list_for_job_groups_keys(session: AsyncSession) -> None:
    await _seed_source(session)
    await _wipe_watermarks(session)
    store = WatermarkStore(session)
    await store.advance("kap", "j", "a", "1", None)
    await store.advance("kap", "j", "b", "2", None)
    await store.advance("kap", "other", "c", "9", None)
    await session.commit()
    rows = await store.list_for_job("kap", "j")
    assert rows == {"a": "1", "b": "2"}


# ─── audit-content tests for Task 9 ────────────────────────────────────


async def test_advance_emits_watermark_advance_event_on_success(
    session: AsyncSession,
) -> None:
    """advance() success emits one watermark.advance event with
    target_table='watermark' (codex F2). CAS-miss → no event."""
    from aslan_core.audit import Actor, set_actor

    await _seed_source(session)
    await _wipe_watermarks(session)
    await session.execute(text("DELETE FROM audit.events"))
    await session.commit()

    set_actor(Actor(actor_id="user:wm", actor_kind="user"))
    store = WatermarkStore(session)

    # Insert path.
    ok = await store.advance("kap", "j", "k", new_cursor="1", expected_cursor=None)
    await session.commit()
    assert ok is True

    # CAS path.
    ok = await store.advance("kap", "j", "k", new_cursor="2", expected_cursor="1")
    await session.commit()
    assert ok is True

    # CAS miss → no event.
    ok = await store.advance("kap", "j", "k", new_cursor="3", expected_cursor="bogus")
    await session.commit()
    assert ok is False

    events = (
        await session.execute(
            text(
                "SELECT operation, before, after, actor_id FROM audit.events "
                "WHERE target_schema = 'src' AND target_table = 'watermark' "
                "ORDER BY occurred_at"
            )
        )
    ).all()
    assert [e.operation for e in events] == [
        "watermark.advance",
        "watermark.advance",
    ]
    assert events[0].before is None
    assert events[0].after["cursor_value"] == "1"
    assert events[1].before["cursor_value"] == "1"
    assert events[1].after["cursor_value"] == "2"
    for e in events:
        assert e.actor_id == "user:wm"

    # Row's denormalised audit cols.
    row = (
        await session.execute(
            text(
                "SELECT actor_id FROM src.watermark "
                "WHERE source_id = 'kap' AND job_name = 'j' AND key = 'k'"
            )
        )
    ).one()
    assert row.actor_id == "user:wm"

    set_actor(None)


async def test_set_idempotent_hit_preserves_original_attribution(
    session: AsyncSession,
) -> None:
    """Codex F1: a second actor calling set() with the same cursor
    value as the existing row must NOT rewrite the row's audit cols.
    Emits watermark.idempotent_hit; first writer's actor stays on the
    row."""
    from aslan_core.audit import Actor, set_actor

    await _seed_source(session)
    await _wipe_watermarks(session)
    await session.execute(text("DELETE FROM audit.events"))
    await session.commit()

    set_actor(Actor(actor_id="user:first", actor_kind="user"))
    store = WatermarkStore(session)
    await store.set("kap", "j", "k", "5")
    await session.commit()

    set_actor(Actor(actor_id="user:retry", actor_kind="user"))
    await store.set("kap", "j", "k", "5")  # same value → idempotent hit
    await session.commit()

    row = (
        await session.execute(
            text(
                "SELECT actor_id FROM src.watermark "
                "WHERE source_id = 'kap' AND job_name = 'j' AND key = 'k'"
            )
        )
    ).one()
    assert row.actor_id == "user:first"

    events = (
        await session.execute(
            text(
                "SELECT operation, actor_id FROM audit.events "
                "WHERE target_schema = 'src' AND target_table = 'watermark' "
                "ORDER BY occurred_at"
            )
        )
    ).all()
    assert [e.operation for e in events] == [
        "watermark.set",
        "watermark.idempotent_hit",
    ]
    assert events[0].actor_id == "user:first"
    assert events[1].actor_id == "user:retry"

    set_actor(None)


async def test_set_force_overwrite_emits_force_set_event(
    session: AsyncSession,
) -> None:
    """set() with a different value than the existing row emits
    watermark.force_set with before/after. Last-writer-wins on the
    row's audit cols."""
    from aslan_core.audit import Actor, set_actor

    await _seed_source(session)
    await _wipe_watermarks(session)
    await session.execute(text("DELETE FROM audit.events"))
    await session.commit()

    set_actor(Actor(actor_id="user:first", actor_kind="user"))
    store = WatermarkStore(session)
    await store.advance("kap", "j", "k", new_cursor="10", expected_cursor=None)
    await session.commit()

    set_actor(Actor(actor_id="user:repair", actor_kind="user"))
    await store.set("kap", "j", "k", "1")  # regression — force_set
    await session.commit()

    events = (
        await session.execute(
            text(
                "SELECT operation, before, after, actor_id FROM audit.events "
                "WHERE target_schema = 'src' AND target_table = 'watermark' "
                "  AND operation = 'watermark.force_set'"
            )
        )
    ).all()
    assert len(events) == 1
    assert events[0].before["cursor_value"] == "10"
    assert events[0].after["cursor_value"] == "1"
    assert events[0].actor_id == "user:repair"

    # Row last-writer-wins on audit cols.
    row = (
        await session.execute(
            text(
                "SELECT actor_id FROM src.watermark "
                "WHERE source_id = 'kap' AND job_name = 'j' AND key = 'k'"
            )
        )
    ).one()
    assert row.actor_id == "user:repair"

    set_actor(None)
