"""Integration tests — ``aslan ts series`` subcommands (v0.4.0 Task 23).

Each subcommand round-trips through Click's :class:`CliRunner` so the
full @group → @group → @command auto-actor chain runs. Verifies:

* ``upsert`` creates a series + emits the ``series.upsert`` audit row
  with an ``actor_id`` matching the CLI auto-actor pattern
  ``cli:<user>@<host>``.
* ``get`` round-trips a freshly-upserted series.
* ``get`` on an unknown code reports "not found" but exits 0 (mirrors
  ``aslan doc find`` behaviour — absence is not an error).
* ``list`` returns the upserted series in JSON.
* ``stats`` aggregates by ``(source_id, frequency)``.
* ``upsert`` with ``--subject`` round-trips into ``ts.series_subject``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import click
import pytest
from click.testing import CliRunner, Result
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _cleanup_ts(session: AsyncSession) -> AsyncIterator[None]:
    """The CLI commits via its own session; wipe ts.* + audit rows the
    CLI committed so downstream tests start clean."""
    yield
    await session.rollback()
    for stmt in [
        "DELETE FROM audit.observation_batch_keys",
        "DELETE FROM ts.observation",
        "DELETE FROM ts.series_subject",
        "DELETE FROM ts.series_catalog",
        "DELETE FROM audit.events",
        "DELETE FROM src.ingestion_run",
    ]:
        await session.execute(text(stmt))
    await session.commit()


async def _seed_sources(session: AsyncSession) -> None:
    for stmt in [
        "DELETE FROM audit.observation_batch_keys",
        "DELETE FROM ts.observation",
        "DELETE FROM ts.series_subject",
        "DELETE FROM ts.series_catalog",
        "DELETE FROM audit.events",
        "DELETE FROM src.ingestion_run",
    ]:
        await session.execute(text(stmt))
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('kap', 'KAP', 'scraper', 'open') ON CONFLICT DO NOTHING"
        )
    )
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('manual', 'Manual', 'manual', 'open') ON CONFLICT DO NOTHING"
        )
    )
    await session.commit()


async def _invoke(cli: click.Command, args: list[str]) -> Result:
    runner = CliRunner()
    return await asyncio.to_thread(runner.invoke, cli, args)


async def test_ts_series_upsert_creates_row_with_cli_actor(session: AsyncSession) -> None:
    """``aslan ts series upsert`` lands a row in ``ts.series_catalog``
    AND emits an ``audit.events`` row whose ``actor_id`` starts with
    ``cli:`` — proving the auto-actor wired by the v0.3.0 ``cli`` group
    inherits through to the new ``ts`` subgroup."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)

    result = await _invoke(
        cli,
        [
            "ts",
            "series",
            "upsert",
            "--code",
            "kap.gdp.q",
            "--source-id",
            "kap",
            "--metric",
            "gdp",
            "--frequency",
            "1q",
            "--unit",
            "TRY",
            "--description",
            "Quarterly GDP",
            "--json",
        ],
    )
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    payload = json.loads(result.output)
    assert payload["created"] is True
    assert isinstance(payload["series_id"], int)

    await session.rollback()
    row = (
        await session.execute(
            text(
                "SELECT series_id, source_id, metric, frequency, unit, description "
                "FROM ts.series_catalog WHERE series_code = :code"
            ),
            {"code": "kap.gdp.q"},
        )
    ).first()
    assert row is not None
    assert row.source_id == "kap"
    assert row.metric == "gdp"
    assert row.frequency == "1q"

    # Audit row carries the CLI auto-actor.
    actor_id = await session.scalar(
        text(
            "SELECT actor_id FROM audit.events "
            "WHERE operation = 'series.upsert' "
            "  AND (target_pk->>'series_id')::int = :sid"
        ),
        {"sid": row.series_id},
    )
    assert actor_id is not None
    assert actor_id.startswith("cli:"), (
        f"audit row should be tagged with the CLI auto-actor; got {actor_id!r}"
    )


async def test_ts_series_get_round_trips(session: AsyncSession) -> None:
    """``upsert`` then ``get`` returns the same series."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)

    upsert = await _invoke(
        cli,
        [
            "ts",
            "series",
            "upsert",
            "--code",
            "kap.cpi.m",
            "--source-id",
            "kap",
            "--metric",
            "cpi",
            "--frequency",
            "1mo",
            "--unit",
            "index",
            "--json",
        ],
    )
    assert upsert.exit_code == 0, upsert.output

    get_res = await _invoke(cli, ["ts", "series", "get", "kap.cpi.m", "--json"])
    assert get_res.exit_code == 0, get_res.output
    series = json.loads(get_res.output)
    assert series["series_code"] == "kap.cpi.m"
    assert series["source_id"] == "kap"
    assert series["metric"] == "cpi"
    assert series["frequency"] == "1mo"
    assert series["unit"] == "index"
    assert series["subjects"] == []


async def test_ts_series_get_unknown_code_reports_missing(session: AsyncSession) -> None:
    """``aslan ts series get`` on a missing code prints a not-found
    marker but exits 0 — mirrors ``aslan doc find`` (absence is not
    an error in a read query)."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)

    result = await _invoke(cli, ["ts", "series", "get", "no.such.code", "--json"])
    assert result.exit_code == 0
    # JSON null on missing.
    assert json.loads(result.output) is None


async def test_ts_series_list_returns_upserted(session: AsyncSession) -> None:
    """``aslan ts series list`` returns rows from the catalog."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)

    for code, freq in [("kap.a", "1d"), ("kap.b", "1mo"), ("kap.c", "1q")]:
        r = await _invoke(
            cli,
            [
                "ts",
                "series",
                "upsert",
                "--code",
                code,
                "--source-id",
                "kap",
                "--metric",
                "m",
                "--frequency",
                freq,
                "--unit",
                "TRY",
                "--json",
            ],
        )
        assert r.exit_code == 0, r.output

    result = await _invoke(cli, ["ts", "series", "list", "--source-id", "kap", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    codes = {r["series_code"] for r in rows}
    assert {"kap.a", "kap.b", "kap.c"} <= codes


async def test_ts_series_stats_aggregates_by_source_and_frequency(
    session: AsyncSession,
) -> None:
    """``aslan ts series stats`` returns aggregate counts."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)

    for code, freq in [
        ("kap.a", "1d"),
        ("kap.b", "1d"),
        ("kap.c", "1mo"),
    ]:
        r = await _invoke(
            cli,
            [
                "ts",
                "series",
                "upsert",
                "--code",
                code,
                "--source-id",
                "kap",
                "--metric",
                "m",
                "--frequency",
                freq,
                "--unit",
                "TRY",
                "--json",
            ],
        )
        assert r.exit_code == 0, r.output

    result = await _invoke(cli, ["ts", "series", "stats", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    by_freq = {r["frequency"]: r["count"] for r in rows if r["source_id"] == "kap"}
    assert by_freq.get("1d") == 2
    assert by_freq.get("1mo") == 1


async def test_ts_series_upsert_with_subject_persists_subject(
    session: AsyncSession,
) -> None:
    """``--subject subject_id:role`` round-trips into
    ``ts.series_subject``. Identifying-class series must declare at
    least one subject; passing one through the CLI exercises the
    multi-value parser."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)

    result = await _invoke(
        cli,
        [
            "ts",
            "series",
            "upsert",
            "--code",
            "kap.exec.comp",
            "--source-id",
            "kap",
            "--metric",
            "comp",
            "--frequency",
            "1y",
            "--unit",
            "TRY",
            "--pii-class",
            "identifying",
            "--subject",
            "kap-person-001:executive",
            "--subject",
            "kap-person-002:board_member",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    series_id = payload["series_id"]

    await session.rollback()
    rows = (
        await session.execute(
            text(
                "SELECT subject_id, role FROM ts.series_subject "
                "WHERE series_id = :sid ORDER BY subject_id"
            ),
            {"sid": series_id},
        )
    ).all()
    assert [(r.subject_id, r.role) for r in rows] == [
        ("kap-person-001", "executive"),
        ("kap-person-002", "board_member"),
    ]


async def test_ts_series_upsert_unknown_source_id_exits_nonzero(
    session: AsyncSession,
) -> None:
    """An unknown ``--source-id`` fails the FK on the catalog INSERT
    and exits non-zero so operator typos are surfaced."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)

    result = await _invoke(
        cli,
        [
            "ts",
            "series",
            "upsert",
            "--code",
            "x.y.z",
            "--source-id",
            "no-such-source",
            "--metric",
            "m",
            "--frequency",
            "1d",
            "--unit",
            "TRY",
            "--json",
        ],
    )
    assert result.exit_code != 0
