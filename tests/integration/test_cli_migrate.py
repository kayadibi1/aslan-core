from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


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
    """Subprocess the CLI with Redis/S3 env vars stripped."""
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


def test_aslan_migrate_history(pg_dsn: str) -> None:
    proc = _run(["migrate", "history"], {"ASLAN_PG_DSN": pg_dsn})
    assert proc.returncode == 0, proc.stderr
    assert "0001" in proc.stdout or "0001" in proc.stderr
    assert "0003" in proc.stdout or "0003" in proc.stderr


def test_aslan_migrate_up_is_idempotent(pg_dsn: str) -> None:
    """Running `migrate up` against an already-current DB is a no-op."""
    proc = _run(["migrate", "up"], {"ASLAN_PG_DSN": pg_dsn})
    assert proc.returncode == 0, proc.stderr


def test_aslan_migrate_history_works_without_redis_or_s3_env(pg_dsn: str) -> None:
    """Migrations resolve the DSN from env without requiring Redis/S3 settings."""
    proc = _run_db_only(["migrate", "history"], pg_dsn)
    assert proc.returncode == 0, proc.stderr
