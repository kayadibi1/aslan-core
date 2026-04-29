"""Integration tests — ``aslan ts observation`` subcommands (Task 24).

Verifies:

* CSV ``write`` happy path (3 observations) round-trips to
  ``ts.observation`` and emits ``observation.write_batch`` audit row
  tagged with the ``cli:`` auto-actor.
* CSV with a naive datetime exits non-zero (parser rejects).
* CSV with both ``value`` and ``value_text`` populated exits non-zero.
* ``write`` against an unknown ``--series-code`` exits non-zero.
* ``latest`` round-trips a known observation.
* ``range`` returns rows in ts ASC order with PIT collapse.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import click
import pytest
from click.testing import CliRunner, Result
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _cleanup_ts(session: AsyncSession) -> AsyncIterator[None]:
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
    await session.commit()


async def _invoke(cli: click.Command, args: list[str]) -> Result:
    runner = CliRunner()
    return await asyncio.to_thread(runner.invoke, cli, args)


async def _upsert_series(cli: click.Command, code: str) -> int:
    """Helper: upsert a series via the CLI and return the series_id."""
    res = await _invoke(
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
            "x",
            "--frequency",
            "1d",
            "--unit",
            "TRY",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    return int(json.loads(res.output)["series_id"])


async def test_observation_write_csv_inserts_three_rows_with_cli_actor(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    """CSV happy path: three rows land in ``ts.observation``, the
    write-batch audit event carries the ``cli:`` auto-actor, and the
    counters in ``WriteCount`` match the request."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)
    series_id = await _upsert_series(cli, "kap.metric.day")

    csv_path = tmp_path / "obs.csv"
    csv_path.write_text(
        "ts,as_of,value,quality_flag,metadata\n"
        "2026-04-01T00:00:00Z,2026-04-01T01:00:00Z,1.5,0,\n"
        "2026-04-02T00:00:00Z,2026-04-02T01:00:00Z,2.5,0,\n"
        '2026-04-03T00:00:00Z,2026-04-03T01:00:00Z,3.5,0,"{""src"":""manual""}"\n'
    )

    result = await _invoke(
        cli,
        [
            "ts",
            "observation",
            "write",
            "--series-code",
            "kap.metric.day",
            "--csv",
            str(csv_path),
            "--json",
        ],
    )
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    payload = json.loads(result.output)
    assert payload["attempted"] == 3
    assert payload["inserted"] == 3
    assert payload["unchanged"] == 0
    assert payload["updated"] == 0

    # Rows landed in ts.observation.
    await session.rollback()
    n = await session.scalar(
        text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
        {"sid": series_id},
    )
    assert n == 3

    # write_batch audit row tagged with cli:* actor.
    actor_id = await session.scalar(
        text(
            "SELECT actor_id FROM audit.events "
            "WHERE operation = 'observation.write_batch' "
            "  AND (target_pk->>'series_id')::int = :sid"
        ),
        {"sid": series_id},
    )
    assert actor_id is not None
    assert actor_id.startswith("cli:"), (
        f"observation.write_batch audit row should be tagged with cli:* actor; got {actor_id!r}"
    )


async def test_observation_write_csv_naive_datetime_rejects(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A CSV row whose ``ts`` lacks a tz-suffix (`Z` / `+00:00`) is
    rejected at parse time — exit code non-zero, no rows inserted."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)
    series_id = await _upsert_series(cli, "kap.metric.naive")

    csv_path = tmp_path / "obs_naive.csv"
    # Note the second row's ts has no tz suffix — parser must reject.
    csv_path.write_text(
        "ts,as_of,value\n"
        "2026-04-01T00:00:00Z,2026-04-01T01:00:00Z,1.0\n"
        "2026-04-02T00:00:00,2026-04-02T01:00:00Z,2.0\n"
    )
    result = await _invoke(
        cli,
        [
            "ts",
            "observation",
            "write",
            "--series-code",
            "kap.metric.naive",
            "--csv",
            str(csv_path),
            "--json",
        ],
    )
    assert result.exit_code != 0, f"naive datetime should have been rejected: {result.output}"

    # No rows inserted (pre-flight failure before any DB I/O).
    await session.rollback()
    n = await session.scalar(
        text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
        {"sid": series_id},
    )
    assert n == 0


async def test_observation_write_csv_value_and_value_text_rejects(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A CSV row populating both ``value`` and ``value_text`` is
    rejected (the spec contract is exactly-one)."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)
    series_id = await _upsert_series(cli, "kap.metric.both")

    csv_path = tmp_path / "obs_both.csv"
    csv_path.write_text(
        "ts,as_of,value,value_text\n2026-04-01T00:00:00Z,2026-04-01T01:00:00Z,1.5,extra\n"
    )
    result = await _invoke(
        cli,
        [
            "ts",
            "observation",
            "write",
            "--series-code",
            "kap.metric.both",
            "--csv",
            str(csv_path),
            "--json",
        ],
    )
    assert result.exit_code != 0, (
        f"row with both value and value_text should have been rejected: {result.output}"
    )

    await session.rollback()
    n = await session.scalar(
        text("SELECT COUNT(*) FROM ts.observation WHERE series_id = :sid"),
        {"sid": series_id},
    )
    assert n == 0


async def test_observation_write_unknown_series_code_exits_nonzero(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    """``--series-code`` must reference an existing catalog row; otherwise
    exit non-zero before opening an ingestion_run row."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)

    csv_path = tmp_path / "obs.csv"
    csv_path.write_text("ts,as_of,value\n2026-04-01T00:00:00Z,2026-04-01T01:00:00Z,1.5\n")
    result = await _invoke(
        cli,
        [
            "ts",
            "observation",
            "write",
            "--series-code",
            "no.such.code",
            "--csv",
            str(csv_path),
            "--json",
        ],
    )
    assert result.exit_code != 0


async def test_observation_latest_returns_most_recent(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    """``aslan ts observation latest`` returns the row with the highest
    ``ts``."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)
    await _upsert_series(cli, "kap.latest")

    csv_path = tmp_path / "obs_latest.csv"
    csv_path.write_text(
        "ts,as_of,value\n"
        "2026-04-01T00:00:00Z,2026-04-01T01:00:00Z,1.0\n"
        "2026-04-03T00:00:00Z,2026-04-03T01:00:00Z,3.0\n"
        "2026-04-02T00:00:00Z,2026-04-02T01:00:00Z,2.0\n"
    )
    write = await _invoke(
        cli,
        [
            "ts",
            "observation",
            "write",
            "--series-code",
            "kap.latest",
            "--csv",
            str(csv_path),
            "--json",
        ],
    )
    assert write.exit_code == 0, write.output

    result = await _invoke(cli, ["ts", "observation", "latest", "kap.latest", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ts"].startswith("2026-04-03T00:00:00")
    assert payload["value"] == 3.0


async def test_observation_range_returns_window_in_ts_order(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    """``aslan ts observation range --start --end`` returns rows in
    ascending ``ts`` order across a half-open window."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)
    await _upsert_series(cli, "kap.range")

    csv_path = tmp_path / "obs_range.csv"
    csv_path.write_text(
        "ts,as_of,value\n"
        "2026-04-01T00:00:00Z,2026-04-01T01:00:00Z,1.0\n"
        "2026-04-02T00:00:00Z,2026-04-02T01:00:00Z,2.0\n"
        "2026-04-03T00:00:00Z,2026-04-03T01:00:00Z,3.0\n"
        "2026-04-04T00:00:00Z,2026-04-04T01:00:00Z,4.0\n"
    )
    write = await _invoke(
        cli,
        [
            "ts",
            "observation",
            "write",
            "--series-code",
            "kap.range",
            "--csv",
            str(csv_path),
            "--json",
        ],
    )
    assert write.exit_code == 0, write.output

    # Half-open [2026-04-02, 2026-04-04) returns rows 2 and 3.
    result = await _invoke(
        cli,
        [
            "ts",
            "observation",
            "range",
            "kap.range",
            "--start",
            "2026-04-02T00:00:00Z",
            "--end",
            "2026-04-04T00:00:00Z",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 2
    assert rows[0]["ts"].startswith("2026-04-02")
    assert rows[1]["ts"].startswith("2026-04-03")
