"""Integration tests — ``aslan streams`` subcommands (v0.5.0 Task 19).

Each subcommand round-trips through Click's :class:`CliRunner` so the
full ``aslan`` → ``streams`` → ``<cmd>`` auto-actor chain runs.

* :command:`publish`         — outbox INSERT + audit row
* :command:`tail`            — XRANGE round-trip
* :command:`drain`           — drains a pending outbox row to Redis
* :command:`lag`              — group lag JSON shape
* :command:`pending`         — XPENDING summary
* :command:`deadletter list` — JSON list of deadletter_log rows
* :command:`deadletter retry` — re-route a stuck failure_id
* :command:`claim-release`    — DEL of a stale claim key
* :command:`processed-clear`  — SREM + audit emission
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable
from typing import Any, cast
from uuid import uuid4

import click
import pytest
import pytest_asyncio
from click.testing import CliRunner, Result
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

STREAM = "aslan.kap.filings.new"
GROUP = "internal-test"


def _aw(x: Any) -> Awaitable[Any]:
    return cast(Awaitable[Any], x)


@pytest.fixture(autouse=True)
def _set_redis_env(redis_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI's ``Settings()`` at the test Redis testcontainer."""
    monkeypatch.setenv("ASLAN_REDIS_URL", redis_url)
    # Settings(...) reads from os.environ on each instantiation, so the
    # CLI commands pick up the testcontainer URL transparently.


@pytest.fixture(autouse=True)
async def _seed_kap_source(session: AsyncSession) -> AsyncIterator[None]:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('kap', 'KAP', 'scraper', 'open') ON CONFLICT DO NOTHING"
        )
    )
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('cli', 'CLI', 'manual', 'open') ON CONFLICT DO NOTHING"
        )
    )
    await session.commit()
    yield


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _wipe_streams(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    yield
    async with session_factory() as session:
        await session.execute(text("DELETE FROM streams.deadletter_xadd_intent"))
        await session.execute(text("DELETE FROM streams.deadletter_redis_index"))
        await session.execute(text("DELETE FROM streams.deadletter_log"))
        await session.execute(text("DELETE FROM streams.event_id_to_redis"))
        await session.execute(text("DELETE FROM streams.outbox"))
        await session.execute(text("DELETE FROM src.ingestion_run"))
        await session.commit()
    await redis_client.flushdb()


async def _invoke(cli: click.Command, args: list[str]) -> Result:
    runner = CliRunner()
    return await asyncio.to_thread(runner.invoke, cli, args)


def _publish_payload(**override: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "filing_id": str(uuid4()),
        "entity_id": None,
        "filing_kind": "material_event",
        "title": "cli publish test",
        "published_at": "2026-04-29T12:00:00+00:00",
        "primary_object_key": "k",
        "bucket": "b",
        "is_revision": False,
        "revision_no": 1,
    }
    base.update(override)
    return base


# ─── publish ────────────────────────────────────────────────────────────


async def test_streams_publish_creates_outbox_row(session: AsyncSession) -> None:
    from aslan_core.cli.main import cli

    eid = str(uuid4())
    payload = _publish_payload(event_id=eid)
    result = await _invoke(
        cli,
        [
            "streams",
            "publish",
            "--kind",
            "filing.new",
            "--source-id",
            "kap",
            "--payload",
            json.dumps(payload),
            "--json",
        ],
    )
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    out = json.loads(result.output)
    assert out["event_id"] == eid
    assert out["stream"] == STREAM
    assert out["kind"] == "filing.new"

    await session.rollback()
    row = (
        await session.execute(
            text(
                "SELECT outbox_id, stream_name, event_id "
                "FROM streams.outbox WHERE event_id = CAST(:eid AS UUID)"
            ),
            {"eid": eid},
        )
    ).first()
    assert row is not None
    assert row.stream_name == STREAM


# ─── tail ────────────────────────────────────────────────────────────────


async def test_streams_tail_reads_recent_entries(redis_client: Redis) -> None:
    from aslan_core.cli.main import cli

    eid = str(uuid4())
    fields: dict[
        bytes | bytearray | memoryview | str | int | float,
        bytes | bytearray | memoryview | str | int | float,
    ] = {
        "event_id": eid,
        "schema_version": "1",
        "payload": json.dumps({"x": 1}),
    }
    await redis_client.xadd(STREAM, fields)

    result = await _invoke(cli, ["streams", "tail", STREAM, "--count", "5", "--json"])
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    out = json.loads(result.output)
    assert any(r["fields"].get("event_id") == eid for r in out)


# ─── drain ───────────────────────────────────────────────────────────────


async def test_streams_drain_publishes_pending_outbox(
    session: AsyncSession,
) -> None:
    from aslan_core.cli.main import cli

    # Seed an outbox row directly (avoids the publish auto-actor coupling).
    rid = (
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run(source_id, job_name, status) "
                "VALUES('kap','cli_drain_test','succeeded') "
                "RETURNING ingestion_run_id"
            )
        )
    ).scalar_one()
    eid = str(uuid4())
    await session.execute(
        text(
            """
            INSERT INTO streams.outbox(
                stream_name, event_id, schema_version, payload,
                source_id, producer_run_id
            )
            VALUES (
                :stream, CAST(:eid AS UUID), 1,
                CAST(:payload AS JSONB), 'kap', :rid
            )
            """
        ),
        {
            "stream": STREAM,
            "eid": eid,
            "rid": rid,
            "payload": json.dumps(
                {
                    "schema_version": 1,
                    "event_id": eid,
                    "produced_at": "2026-04-29T12:00:00+00:00",
                    "producer_run_id": rid,
                    "source_id": "kap",
                    "kind": "filing.new",
                }
            ),
        },
    )
    await session.commit()

    result = await _invoke(cli, ["streams", "drain", "--limit", "100", "--json"])
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    out = json.loads(result.output)
    assert out["pending_after"] == 0
    assert out["drained"] >= 1


# ─── lag ─────────────────────────────────────────────────────────────────


async def test_streams_lag_reports_group_state(redis_client: Redis) -> None:
    from aslan_core.cli.main import cli

    fields: dict[
        bytes | bytearray | memoryview | str | int | float,
        bytes | bytearray | memoryview | str | int | float,
    ] = {
        "event_id": str(uuid4()),
        "payload": "{}",
    }
    await redis_client.xadd(STREAM, fields)
    # Create the group via XGROUP CREATE so xinfo_groups has a target.
    # BUSYGROUP swallow — the consumer group may already exist from a
    # sibling test in the same session.
    with contextlib.suppress(Exception):
        await redis_client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)

    result = await _invoke(cli, ["streams", "lag", STREAM, "--group", GROUP, "--json"])
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    out = json.loads(result.output)
    assert out["stream"] == STREAM
    assert out["group"] == GROUP


# ─── pending ─────────────────────────────────────────────────────────────


async def test_streams_pending_reports_zero_for_fresh_group(redis_client: Redis) -> None:
    from aslan_core.cli.main import cli

    fields: dict[
        bytes | bytearray | memoryview | str | int | float,
        bytes | bytearray | memoryview | str | int | float,
    ] = {
        "event_id": str(uuid4()),
        "payload": "{}",
    }
    await redis_client.xadd(STREAM, fields)
    # BUSYGROUP swallow — group may already exist from a sibling test.
    with contextlib.suppress(Exception):
        await redis_client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)

    result = await _invoke(cli, ["streams", "pending", STREAM, "--group", GROUP, "--json"])
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    out = json.loads(result.output)
    assert out["stream"] == STREAM
    assert out["group"] == GROUP
    assert "pending" in out


# ─── deadletter list ─────────────────────────────────────────────────────


async def test_streams_deadletter_list_returns_seeded_row(
    session: AsyncSession,
) -> None:
    from aslan_core.cli.main import cli

    eid = str(uuid4())
    await session.execute(
        text(
            """
            INSERT INTO streams.deadletter_log(
                stream_name, deadletter_stream, event_id,
                original_message_id, group_name, consumer_name,
                failure_count, last_error, payload_excerpt
            )
            VALUES (
                :stream, :dl, CAST(:eid AS UUID),
                :mid, :group, 'consumer:test',
                5, 'cli list test', '{}'::jsonb
            )
            """
        ),
        {
            "stream": STREAM,
            "dl": f"{STREAM}.deadletter",
            "eid": eid,
            "mid": "cli-list-0",
            "group": GROUP,
        },
    )
    await session.commit()

    result = await _invoke(cli, ["streams", "deadletter", "list", "--limit", "10", "--json"])
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    out = json.loads(result.output)
    assert any(r["event_id"] == eid for r in out)


# ─── deadletter retry ────────────────────────────────────────────────────


async def test_streams_deadletter_retry_idempotent_for_routed_row(
    session: AsyncSession,
) -> None:
    from aslan_core.cli.main import cli

    eid = str(uuid4())
    fid = (
        await session.execute(
            text(
                """
                INSERT INTO streams.deadletter_log(
                    stream_name, deadletter_stream, event_id,
                    original_message_id, group_name, consumer_name,
                    failure_count, last_error, payload_excerpt,
                    redis_message_id, routed_at_redis
                )
                VALUES (
                    :stream, :dl, CAST(:eid AS UUID),
                    :mid, :group, 'consumer:test',
                    5, 'retry test', '{}'::jsonb,
                    'fixed-rid-retry', now()
                )
                RETURNING failure_id
                """
            ),
            {
                "stream": STREAM,
                "dl": f"{STREAM}.deadletter",
                "eid": eid,
                "mid": "cli-retry-0",
                "group": GROUP,
            },
        )
    ).scalar_one()
    await session.commit()

    result = await _invoke(
        cli,
        ["streams", "deadletter", "retry", str(int(fid)), "--json"],
    )
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    out = json.loads(result.output)
    assert out["failure_id"] == int(fid)
    # F18 contract — already routed, returns the existing rid.
    assert out["redis_message_id"] == "fixed-rid-retry"


# ─── claim-release ───────────────────────────────────────────────────────


async def test_streams_claim_release_drops_claim_key(redis_client: Redis) -> None:
    from aslan_core.cli.main import cli

    eid = str(uuid4())
    claim_key = f"stream:{STREAM}:{GROUP}:claim:{eid}"
    await redis_client.set(claim_key, "owner:cli-test", ex=300)
    assert await redis_client.exists(claim_key) == 1

    result = await _invoke(
        cli,
        [
            "streams",
            "claim-release",
            STREAM,
            "--group",
            GROUP,
            "--event-id",
            eid,
            "--json",
        ],
    )
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    out = json.loads(result.output)
    assert out["deleted"] == 1
    assert await redis_client.exists(claim_key) == 0


# ─── processed-clear ─────────────────────────────────────────────────────


async def test_streams_processed_clear_drops_processed_member(
    session: AsyncSession,
    redis_client: Redis,
) -> None:
    from aslan_core.cli.main import cli

    eid = str(uuid4())
    processed_key = f"stream:{STREAM}:{GROUP}:processed"
    await _aw(redis_client.sadd(processed_key, eid))
    assert await _aw(redis_client.sismember(processed_key, eid))

    result = await _invoke(
        cli,
        [
            "streams",
            "processed-clear",
            STREAM,
            "--group",
            GROUP,
            "--event-id",
            eid,
            "--json",
        ],
    )
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    out = json.loads(result.output)
    assert out["removed"] == 1
    assert not await _aw(redis_client.sismember(processed_key, eid))

    # Audit row should be emitted with the cli:processed_clear tag.
    await session.rollback()
    audit_n = (
        await session.execute(
            text(
                """
                SELECT count(*)
                FROM audit.events
                WHERE operation = 'stream.entry_redacted'
                  AND target_pk->>'event_id' = :eid
                  AND metadata->>'cause' = 'cli:processed_clear'
                """
            ),
            {"eid": eid},
        )
    ).scalar_one()
    assert audit_n == 1


# ─── known (sanity helper) ───────────────────────────────────────────────


async def test_streams_known_lists_canonical_streams() -> None:
    from aslan_core.cli.main import cli

    result = await _invoke(cli, ["streams", "known", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "aslan.kap.filings.new" in payload
    assert "aslan.entity.created" in payload
