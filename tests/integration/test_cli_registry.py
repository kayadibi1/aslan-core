from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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


async def _wipe(session: AsyncSession) -> None:
    for stmt in [
        "DELETE FROM ref.entity_sector",
        "DELETE FROM ref.entity_relationship",
        "DELETE FROM ref.identifier",
        "DELETE FROM ref.entity",
        "DELETE FROM ref.sector",
        "DELETE FROM src.ingestion_run",
        "DELETE FROM src.source",
    ]:
        await session.execute(text(stmt))
    await session.commit()


async def _seed_manual_source(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('manual', 'Manual', 'manual', 'open') ON CONFLICT DO NOTHING"
        )
    )
    await session.commit()


async def test_create_resolve_roundtrip(pg_dsn: str, session: AsyncSession) -> None:
    await _wipe(session)
    await _seed_manual_source(session)

    proc = _run(
        [
            "registry",
            "create-entity",
            "--type",
            "company",
            "--name",
            "Test A.Ş.",
            "--identifier",
            "kap_entity_code=99999",
            "--identifier",
            "bist_ticker=TEST",
        ],
        {"ASLAN_PG_DSN": pg_dsn},
    )
    assert proc.returncode == 0, proc.stderr

    proc = _run(
        ["registry", "resolve", "--namespace", "bist_ticker", "--value", "TEST", "--json"],
        {"ASLAN_PG_DSN": pg_dsn},
    )
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert "entity_id" in parsed
    assert parsed["entity_id"] is not None


async def test_resolve_missing_returns_null(pg_dsn: str, session: AsyncSession) -> None:
    await _wipe(session)
    await _seed_manual_source(session)
    proc = _run(
        ["registry", "resolve", "--namespace", "bist_ticker", "--value", "GHOST", "--json"],
        {"ASLAN_PG_DSN": pg_dsn},
    )
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert parsed == {"entity_id": None}


async def test_identifiers_command_lists_namespaces(
    pg_dsn: str,
    session: AsyncSession,
) -> None:
    await _wipe(session)
    await _seed_manual_source(session)

    create_proc = _run(
        [
            "registry",
            "create-entity",
            "--type",
            "company",
            "--name",
            "Probe A.Ş.",
            "--identifier",
            "kap_entity_code=42",
            "--identifier",
            "bist_ticker=PROBE",
        ],
        {"ASLAN_PG_DSN": pg_dsn},
    )
    assert create_proc.returncode == 0, create_proc.stderr
    eid = json.loads(create_proc.stdout)["entity_id"]

    proc = _run(
        ["registry", "identifiers", "--entity", eid, "--json"],
        {"ASLAN_PG_DSN": pg_dsn},
    )
    assert proc.returncode == 0, proc.stderr
    ids = json.loads(proc.stdout)
    assert ids == {"kap_entity_code": "42", "bist_ticker": "PROBE"}


async def test_cli_create_entity_audit_row_has_cli_actor(
    pg_dsn: str,
    session: AsyncSession,
) -> None:
    """The root cli() group sets the actor from getpass.getuser() +
    socket.gethostname() so manual operations don't need a flag.

    Task 18, v0.3.0: every audit row written by the CLI subprocess
    should carry an actor_id starting with ``cli:`` — proving the
    callback ran and populated the ContextVar before any mutation.
    Note: the autouse ``_default_test_actor`` fixture in this test
    process sets ``user:pytest`` but that does NOT cross subprocess
    boundaries, so the CLI's own actor-set is what lands on the row.
    """
    await _wipe(session)
    await session.execute(text("DELETE FROM audit.events"))
    await session.commit()
    await _seed_manual_source(session)

    proc = _run(
        [
            "registry",
            "create-entity",
            "--type",
            "company",
            "--name",
            "Actor Probe A.Ş.",
            "--identifier",
            "kap_entity_code=77777",
        ],
        {"ASLAN_PG_DSN": pg_dsn},
    )
    assert proc.returncode == 0, proc.stderr

    rows = (
        await session.execute(
            text(
                "SELECT actor_id, actor_kind FROM audit.events "
                "WHERE target_schema = 'ref' AND target_table = 'entity' "
                "ORDER BY occurred_at DESC LIMIT 1"
            )
        )
    ).all()
    assert len(rows) == 1, "expected one audit row from the CLI mutation"
    assert rows[0].actor_id.startswith("cli:"), (
        f"audit row actor_id should start with 'cli:'; got {rows[0].actor_id!r}"
    )
    assert rows[0].actor_kind == "user"


async def test_search_returns_results(pg_dsn: str, session: AsyncSession) -> None:
    await _wipe(session)
    await _seed_manual_source(session)
    create_proc = _run(
        [
            "registry",
            "create-entity",
            "--type",
            "company",
            "--name",
            "Aselsan A.Ş.",
            "--identifier",
            "kap_entity_code=19387",
        ],
        {"ASLAN_PG_DSN": pg_dsn},
    )
    assert create_proc.returncode == 0, create_proc.stderr

    proc = _run(
        ["registry", "search", "Aselsan", "--type", "company", "--json"],
        {"ASLAN_PG_DSN": pg_dsn},
    )
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    assert isinstance(results, list)
    assert any("Aselsan" in r["legal_name"] for r in results)
