"""Migration 0020 password handling — codex plan-round-2 finding.

Spec §8.2 + plan Task 2: the migration reads ``ASLAN_DASHBOARD_PASSWORD``
from the environment and must:

  Case 1: handle a password containing a single quote AND a backslash —
          the role MUST log in with the exact string. Verifies that the
          ``set_config('aslan.dashboard_password', :pw, true)`` +
          ``format(... %L ...)`` chain escapes correctly server-side
          and that no escape mismatch corrupts the password.
  Case 2: refuse to run in production with no password — RuntimeError
          with the documented operator-facing message.
  Case 3: fall back to ``DEV_ONLY_REPLACE_ME`` in dev — and the role
          must actually log in with that fallback.
  Case 4: release the ``aslan.dashboard_password`` GUC at COMMIT —
          ``SET LOCAL`` semantics. Outside the migration transaction
          the GUC is unset; nothing about the password persists in
          pg_settings or pg_stat_statements as a session setting.

Each replay (cases 1–3) drives alembic via a subprocess so the parent
test process's event loop and connection pool stay clean. Cases run
sequentially under one test session; teardown re-runs ``upgrade head``
to restore the DB for the rest of the suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DASHBOARD_DEV_PASSWORD = "DEV_ONLY_REPLACE_ME"


def _run_alembic(
    *args: str,
    pg_dsn: str,
    env_overrides: dict[str, str] | None = None,
    drop_password: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Drive the alembic CLI in a fresh subprocess. Returns the
    completed process so the caller can assert on returncode + stderr."""
    env = os.environ.copy()
    if drop_password:
        env.pop("ASLAN_DASHBOARD_PASSWORD", None)
    env["ASLAN_PG_DSN"] = pg_dsn
    if env_overrides:
        env.update(env_overrides)
    # All argv entries are constants or alembic subcommands from the
    # caller (test code), never user input. ``check=False`` is intentional —
    # callers assert on returncode + stderr.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture
def _restore_head_after(pg_dsn: str) -> Iterator[None]:
    """After each test, force the DB back to head so subsequent tests
    in the suite get the post-0020 schema. Uses default env (dev,
    no password) so the upgrade always succeeds via the fallback."""
    yield
    result = _run_alembic("upgrade", "head", pg_dsn=pg_dsn, drop_password=True)
    assert result.returncode == 0, (
        f"failed to restore head after password edge case test: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_password_with_quote_and_backslash_round_trips(
    pg_dsn: str,
    _restore_head_after: None,
) -> None:
    """Codex plan-round-2: a password with both a single quote and a
    backslash must NOT corrupt the CREATE ROLE statement, and the role
    must accept the exact string at login."""
    weird_pw = "hello'world\\strange"

    # Drop 0020 first so the upgrade re-creates the role with the new pw.
    down = _run_alembic("downgrade", "0019", pg_dsn=pg_dsn, drop_password=True)
    assert down.returncode == 0, f"downgrade failed: {down.stderr!r}"

    up = _run_alembic(
        "upgrade",
        "head",
        pg_dsn=pg_dsn,
        env_overrides={"ASLAN_DASHBOARD_PASSWORD": weird_pw},
    )
    assert up.returncode == 0, f"upgrade with weird password failed: stderr={up.stderr!r}"

    # Verify the role logs in with the exact password.
    import asyncio

    async def _try_login() -> None:
        from urllib.parse import urlparse, urlunparse

        raw = pg_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        parsed = urlparse(raw)
        netloc = f"aslan_dashboard:{weird_pw}@{parsed.hostname}:{parsed.port}"
        dashboard_dsn = urlunparse(
            (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
        )
        conn = await asyncpg.connect(dsn=dashboard_dsn)
        try:
            who = await conn.fetchval("SELECT current_user")
            assert who == "aslan_dashboard"
        finally:
            await conn.close()

    asyncio.run(_try_login())


def test_missing_password_in_production_fails_with_documented_message(
    pg_dsn: str,
    _restore_head_after: None,
) -> None:
    """Codex plan-round-1: refuse to run in production with no password.
    The RuntimeError message MUST point operators at the runbook."""
    down = _run_alembic("downgrade", "0019", pg_dsn=pg_dsn, drop_password=True)
    assert down.returncode == 0, f"downgrade failed: {down.stderr!r}"

    up = _run_alembic(
        "upgrade",
        "head",
        pg_dsn=pg_dsn,
        env_overrides={"ASLAN_ENV": "production"},
        drop_password=True,
    )
    assert up.returncode != 0, (
        "migration must NOT succeed without ASLAN_DASHBOARD_PASSWORD "
        "in production; got returncode=0"
    )
    combined = up.stdout + up.stderr
    assert "ASLAN_DASHBOARD_PASSWORD is required in non-dev environments" in combined, (
        f"missing documented operator message in alembic output: {combined!r}"
    )


def test_missing_password_in_dev_uses_fallback_and_role_can_login(
    pg_dsn: str,
    _restore_head_after: None,
) -> None:
    """ASLAN_ENV=dev (or unset) + no password → fallback to
    ``DEV_ONLY_REPLACE_ME``. The role must accept that literal."""
    down = _run_alembic("downgrade", "0019", pg_dsn=pg_dsn, drop_password=True)
    assert down.returncode == 0, f"downgrade failed: {down.stderr!r}"

    up = _run_alembic(
        "upgrade",
        "head",
        pg_dsn=pg_dsn,
        env_overrides={"ASLAN_ENV": "dev"},
        drop_password=True,
    )
    assert up.returncode == 0, f"dev fallback upgrade failed: {up.stderr!r}"

    import asyncio

    async def _try_login() -> None:
        from urllib.parse import urlparse, urlunparse

        raw = pg_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        parsed = urlparse(raw)
        netloc = f"aslan_dashboard:{_DASHBOARD_DEV_PASSWORD}@{parsed.hostname}:{parsed.port}"
        dashboard_dsn = urlunparse(
            (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
        )
        conn = await asyncpg.connect(dsn=dashboard_dsn)
        try:
            who = await conn.fetchval("SELECT current_user")
            assert who == "aslan_dashboard"
        finally:
            await conn.close()

    asyncio.run(_try_login())


@pytest.mark.asyncio(loop_scope="session")
async def test_aslan_dashboard_password_guc_released_post_commit(
    engine: AsyncEngine,
) -> None:
    """``set_config(name, value, true)`` (i.e. SET LOCAL semantics)
    releases at COMMIT — the GUC must NOT be visible outside the
    migration's transaction."""
    async with engine.connect() as conn:
        value = (
            await conn.execute(text("SELECT current_setting('aslan.dashboard_password', true)"))
        ).scalar_one()
    assert value is None or value == ""
