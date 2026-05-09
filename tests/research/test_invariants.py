"""Phase 4 — bitemporal invariants script tests.

Drives ``scripts/check_bitemporal_invariants.py`` against the testcontainer
Postgres seeded with minimal data and asserts the script returns exit 0.

Cases (per docs/specs/bitemporal-research-api/TESTPLAN.md):

- TC-061 / TC-066 — bitemporal_trigger_presence: every Class A registry
  row has a BEFORE UPDATE trigger named ``<table>_no_update``.
- TC-062 / TC-068 — bitemporal_table_registry_completeness: every table
  with an ``as_of`` column has a registry row.
- TC-063 — bitemporal_table_registry_present: registry table itself
  exists.
- TC-064 — ts_observation_no_duplicate_as_of: PK forbids duplicates on
  ``(series_id, ts, as_of)``.
- TC-065 — ts_observation_as_of_not_null: every row has an ``as_of``.

Per SCOPE.md D29 (testing strategy) and §6 (acceptance) item (2).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
INVARIANTS_SCRIPT = REPO_ROOT / "scripts" / "check_bitemporal_invariants.py"


def _psycopg_dsn(asyncpg_dsn: str) -> str:
    """The script uses psycopg (sync); rewrite +asyncpg → libpq."""
    if asyncpg_dsn.startswith("postgresql+asyncpg://"):
        return "postgresql://" + asyncpg_dsn[len("postgresql+asyncpg://") :]
    return asyncpg_dsn


def _run_invariants(dsn: str) -> tuple[int, dict]:
    """Invoke the invariants script as a subprocess; parse JSON report."""
    src_path = str(REPO_ROOT / "src")
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{src_path}{os.pathsep}{existing_pp}" if existing_pp else src_path
    )
    proc = subprocess.run(
        [sys.executable, str(INVARIANTS_SCRIPT), "--dsn", dsn, "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"raw_stdout": proc.stdout, "raw_stderr": proc.stderr}
    return proc.returncode, payload


@pytest.mark.asyncio(loop_scope="session")
async def test_invariants_script_clean_after_migrations(
    session: AsyncSession, pg_dsn: str
) -> None:
    """TC-061..065 — script returns exit 0 on a freshly migrated DB.

    Empty Class A tables trivially satisfy every count-based invariant
    (zero duplicates, zero NULL ``as_of``); the registry-presence and
    trigger-presence invariants are satisfied by migrations 0043/0044.
    """
    # Sanity-check the registry has Class A rows seeded by migration 0044.
    n = await session.scalar(
        text("SELECT COUNT(*) FROM aslan_core.bitemporal_table_registry")
    )
    assert n is not None and int(n) >= 5, (
        f"registry should have ≥5 Class A rows from migration 0044, got {n}"
    )

    code, report = _run_invariants(_psycopg_dsn(pg_dsn))
    assert code == 0, f"invariants reported violations: {report}"
    assert report.get("status") == "PASS", report
    assert report.get("invariants_failed") == 0, report


@pytest.mark.asyncio(loop_scope="session")
async def test_invariants_script_passes_with_seeded_observation(
    session: AsyncSession, pg_dsn: str
) -> None:
    """TC-061..065 — seed a minimal ``ts.observation`` row and re-run.

    Asserts: a non-NULL, unique ``(series_id, ts, as_of)`` row leaves
    every invariant green. The trigger-presence invariant in particular
    confirms ``ts.observation_no_update`` exists and is wired (defends
    against a silent migration regression).
    """
    # Minimum FK chain: src.source → src.ingestion_run → ts.series_catalog.
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('inv_test', 'inv_test', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING"
        )
    )
    run_id = await session.scalar(
        text(
            "INSERT INTO src.ingestion_run (source_id, job_name, status) "
            "VALUES ('inv_test', 'inv_test', 'succeeded') "
            "RETURNING ingestion_run_id"
        )
    )
    await session.execute(
        text(
            "INSERT INTO ts.series_catalog "
            "(series_code, source_id, metric, frequency, unit) "
            "VALUES ('inv_test_series', 'inv_test', 'm', '1d', 'TRY') "
            "ON CONFLICT DO NOTHING"
        )
    )
    series_id = await session.scalar(
        text(
            "SELECT series_id FROM ts.series_catalog "
            "WHERE series_code='inv_test_series'"
        )
    )
    await session.execute(
        text(
            "INSERT INTO ts.observation "
            "(series_id, ts, as_of, value, ingestion_run_id, payload_hash) "
            "VALUES (:sid, '2024-06-15T13:00:00Z', '2024-06-15T13:30:00Z', "
            " 42.10, :rid, repeat('a', 64)) "
            "ON CONFLICT DO NOTHING"
        ),
        {"sid": series_id, "rid": run_id},
    )
    await session.commit()

    try:
        code, report = _run_invariants(_psycopg_dsn(pg_dsn))
        assert code == 0, f"invariants reported violations: {report}"

        # Pull individual invariant statuses from the report.
        results = {r["name"]: r for r in report.get("results", [])}
        for must_be_green in (
            "ts_observation_no_duplicate_as_of",  # TC-064
            "ts_observation_as_of_not_null",  # TC-065
            "bitemporal_table_registry_present",  # TC-063
            "bitemporal_table_registry_completeness",  # TC-062 / TC-068
            "bitemporal_trigger_presence",  # TC-061 / TC-066
        ):
            assert must_be_green in results, f"missing invariant {must_be_green}"
            assert results[must_be_green]["ok"] is True, (
                f"{must_be_green} failed: {results[must_be_green]}"
            )
    finally:
        # Cleanup: keep the testcontainer DB clean for sibling tests.
        await session.execute(
            text("DELETE FROM ts.observation WHERE series_id = :sid"),
            {"sid": series_id},
        )
        await session.execute(
            text(
                "DELETE FROM ts.series_catalog WHERE series_code='inv_test_series'"
            )
        )
        await session.commit()
