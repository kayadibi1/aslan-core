from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from aslan_core.db.engine import create_engine
from aslan_core.db.session import create_session_factory
from aslan_core.documents.object_storage import InMemoryFake as _InMemoryFake

TIMESCALE_IMAGE = "timescale/timescaledb:latest-pg16"


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        TIMESCALE_IMAGE,
        username="aslan",
        password="aslan",  # noqa: S106 — ephemeral test container, not a real credential
        dbname="aslan",
    ) as pg:
        yield pg


@pytest.fixture(scope="session")
def pg_dsn(pg_container: PostgresContainer) -> str:
    raw: str = pg_container.get_connection_url()
    # testcontainers gives us a psycopg2-style URL by default; rewrite to asyncpg.
    if raw.startswith("postgresql+psycopg2://"):
        return "postgresql+asyncpg://" + raw[len("postgresql+psycopg2://") :]
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw[len("postgresql://") :]
    return raw


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations(pg_dsn: str) -> None:
    """Bring the ephemeral DB up to head before any test runs."""
    os.environ["ASLAN_PG_DSN"] = pg_dsn
    os.environ.setdefault("ASLAN_REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault("ASLAN_S3_ENDPOINT", "http://localhost:9000")
    os.environ.setdefault("ASLAN_S3_REGION", "us-east-1")
    os.environ.setdefault("ASLAN_S3_ACCESS_KEY", "x")
    os.environ.setdefault("ASLAN_S3_SECRET_KEY", "x")

    # CLI tests subprocess to `python -m aslan_core.cli.main`; the editable
    # install is unreliable on macOS+uv (uv writes .pth files with the
    # macOS hidden flag, which CPython's site.py skips). Make src/ visible
    # via PYTHONPATH so subprocess interpreters can import aslan_core.
    src_path = str(Path(__file__).resolve().parents[3] / "src")
    existing = os.environ.get("PYTHONPATH", "")
    parts = existing.split(os.pathsep) if existing else []
    if src_path not in parts:
        os.environ["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing}" if existing else src_path

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_dsn)
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine(pg_dsn: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(pg_dsn)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest_asyncio.fixture(loop_scope="session")
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Function-scoped session that rolls back after each test."""
    async with session_factory() as s:
        try:
            yield s
        finally:
            await s.rollback()


@pytest.fixture
def object_storage_fake() -> _InMemoryFake:
    """Function-scoped fresh InMemoryFake. Tests assert against
    fake.all_keys() / fake.get_body() to verify cleanup behavior."""
    return _InMemoryFake()
