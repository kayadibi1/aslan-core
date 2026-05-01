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

v0.6.0 dashboard tests need a connection authenticated as the
``aslan_dashboard`` role. ``aslan_dashboard_dsn`` rewrites the
testcontainer DSN (which logs in as the superuser) to log in as
``aslan_dashboard`` with the dev-fallback password the migration
installed when ``ASLAN_DASHBOARD_PASSWORD`` was unset.
``aslan_dashboard_conn`` opens a fresh raw asyncpg connection per
test on top of that DSN, closes it on teardown.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.audit import Actor, set_actor

# Migration 0020 installs this fallback when ``ASLAN_DASHBOARD_PASSWORD``
# is unset and ``ASLAN_ENV`` is "dev" (the default in the test
# environment). Tests connect with the same literal value.
_DASHBOARD_DEV_PASSWORD = "DEV_ONLY_REPLACE_ME"


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


@pytest.fixture
def aslan_dashboard_dsn(pg_dsn: str) -> str:
    """Rewrite the testcontainer DSN to authenticate as ``aslan_dashboard``.

    The original DSN logs in as the testcontainer superuser; this
    rewrite swaps the user/password so a fresh asyncpg connection
    exercises the actual ``aslan_dashboard`` privilege boundary the
    migration installed (LOGIN, not ``SET ROLE``). Production never
    issues ``SET ROLE`` to reach the dashboard role — the dashboard
    process logs in directly with this credential — so tests use the
    same path.
    """
    raw = pg_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    parsed = urlparse(raw)
    netloc = f"aslan_dashboard:{_DASHBOARD_DEV_PASSWORD}@{parsed.hostname}:{parsed.port}"
    return urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


@pytest_asyncio.fixture(loop_scope="session")
async def aslan_dashboard_conn(
    aslan_dashboard_dsn: str,
) -> AsyncIterator[asyncpg.Connection]:
    """Function-scoped raw asyncpg connection logged in as ``aslan_dashboard``.

    Used by the v0.6.0 boundary tests to assert privilege failures the
    SQLAlchemy session fixture (which is the testcontainer superuser)
    cannot exhibit. Closes on teardown so each test gets a clean
    connection state regardless of test outcome.
    """
    conn = await asyncpg.connect(dsn=aslan_dashboard_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(loop_scope="session")
async def aslan_dashboard_session(
    aslan_dashboard_dsn: str,
) -> AsyncIterator[AsyncSession]:
    """SQLAlchemy AsyncSession authenticated as ``aslan_dashboard``.

    Mirrors the local fixture in
    ``test_dashboard_queries_run_under_aslan_dashboard.py``; promoted
    here so the session-role / read-only / o1-redis-calls tests can
    reuse it. Each test gets a fresh engine; rolled back on teardown
    (the role's ``default_transaction_read_only=on`` means commits
    are no-ops anyway).
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(
        aslan_dashboard_dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            try:
                yield session
            finally:
                await session.rollback()
    finally:
        await engine.dispose()
