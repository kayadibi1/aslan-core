"""Pytest fixtures shared across all integration tests.

Adds the autouse ``_default_test_actor`` fixture so existing tests
that don't care about audit identity don't need to set one
themselves. Tests that exercise the audit-strict-without-actor path
(``tests/integration/test_strict_actor_enforcement.py``) opt out via
a local fixture that re-clears the ContextVar after this autouse
runs.

Also adds ``_wipe_streams_tables_after`` which DELETEs from
``streams.outbox`` and ``streams.event_id_to_redis`` AFTER each test.
v0.5.0 streams tests commit outbox rows that hold a FK reference to
``src.ingestion_run``; without this autouse, any later test that
``DELETE``s ``src.ingestion_run`` (every registry / document / ts
``_wipe`` helper) would fail with a ForeignKeyViolationError.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.audit import Actor, set_actor


@pytest.fixture(autouse=True)
def _default_test_actor() -> Iterator[None]:
    """Set a default test Actor for any mutation that fires during
    integration tests.

    Codex F3, 2026-04-29: this fixture is convenient for the existing
    100+ tests that do not care about audit, but it MUST NOT mask
    the strict-mode contract. Tests that exercise the unset-actor
    path declare a local autouse fixture that runs AFTER this one
    (Pytest applies file-local autouse fixtures after conftest's),
    re-clearing the ContextVar before the test body runs. See
    ``test_strict_actor_enforcement.py`` for the canonical pattern.
    """
    set_actor(Actor(actor_id="user:pytest", actor_kind="user"))
    yield
    set_actor(None)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _wipe_streams_tables_after(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Clean up streams.outbox + streams.event_id_to_redis after every
    integration test so subsequent tests' ``DELETE FROM
    src.ingestion_run`` does not hit the outbox FK.

    Runs as a teardown only — the wipe before each test would race
    with tests that explicitly seed before yielding control. Doing it
    only on teardown still guarantees the next test starts clean.
    """
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM streams.event_id_to_redis"))
        await s.execute(text("DELETE FROM streams.outbox"))
        await s.commit()
