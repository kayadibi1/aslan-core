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
