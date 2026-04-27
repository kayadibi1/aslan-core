from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


FIXTURE = Path(__file__).parent.parent / "fixtures" / "sources_test.yaml"


def _run(args: list[str], env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **env_extra}
    return subprocess.run(  # noqa: S603 — controlled args, not untrusted input
        [sys.executable, "-m", "aslan_core.cli.main", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


_NON_DB_KEYS = (
    "ASLAN_REDIS_URL",
    "ASLAN_S3_ENDPOINT",
    "ASLAN_S3_REGION",
    "ASLAN_S3_ACCESS_KEY",
    "ASLAN_S3_SECRET_KEY",
)


def _run_db_only(args: list[str], pg_dsn: str) -> subprocess.CompletedProcess[str]:
    """Subprocess the CLI with Redis/S3 env vars stripped.

    Proves DB-only commands do not require unrelated subsystem config.
    """
    env = {**os.environ, "ASLAN_PG_DSN": pg_dsn}
    for k in _NON_DB_KEYS:
        env.pop(k, None)
    return subprocess.run(  # noqa: S603 — controlled args, not untrusted input
        [sys.executable, "-m", "aslan_core.cli.main", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


async def _wipe(session: AsyncSession) -> None:
    for stmt in [
        "DELETE FROM ref.entity_sector",
        "DELETE FROM ref.entity_relationship",
        "DELETE FROM ref.identifier",
        "DELETE FROM ref.entity",
        "DELETE FROM ref.sector",
        "DELETE FROM ref.currency",
        "DELETE FROM src.ingestion_run",
        "DELETE FROM src.source",
    ]:
        await session.execute(text(stmt))
    await session.commit()


def test_seed_sources_inserts_rows(pg_dsn: str) -> None:
    proc = _run(
        ["seed", "sources", "--file", str(FIXTURE)],
        {"ASLAN_PG_DSN": pg_dsn},
    )
    assert proc.returncode == 0, proc.stderr


async def test_seed_currencies_seeds_try_usd_eur(
    pg_dsn: str,
    session: AsyncSession,
) -> None:
    await _wipe(session)
    proc = _run(["seed", "currencies"], {"ASLAN_PG_DSN": pg_dsn})
    assert proc.returncode == 0, proc.stderr
    rows = await session.scalar(text("SELECT count(*) FROM ref.currency"))
    assert rows is not None
    assert rows >= 3


async def test_seed_sectors_bist(pg_dsn: str, session: AsyncSession) -> None:
    await _wipe(session)
    proc = _run(
        ["seed", "sectors", "--taxonomy", "bist"],
        {"ASLAN_PG_DSN": pg_dsn},
    )
    assert proc.returncode == 0, proc.stderr
    rows = await session.scalar(text("SELECT count(*) FROM ref.sector WHERE taxonomy = 'bist'"))
    assert rows is not None
    assert rows >= 1


def test_seed_currencies_works_without_redis_or_s3_env(pg_dsn: str) -> None:
    """DB-only seed commands must run without Redis/S3 configuration."""
    proc = _run_db_only(["seed", "currencies"], pg_dsn)
    assert proc.returncode == 0, proc.stderr
