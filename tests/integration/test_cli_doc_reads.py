from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import click
import pytest
from click.testing import CliRunner, Result
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.documents.object_storage import InMemoryFake

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _cleanup_doc_tables(session: AsyncSession) -> AsyncIterator[None]:
    """Each doc-CLI test commits via the CLI's own session, leaving rows
    behind that the test session's rollback can't reach. Wipe doc.* +
    src.ingestion_run rows committed during the test before downstream
    tests start, so DELETE FROM src.ingestion_run elsewhere doesn't trip
    the doc.filing FK."""
    yield
    await session.rollback()
    await session.execute(text("DELETE FROM doc.filing_body"))
    await session.execute(text("DELETE FROM doc.filing_attachment"))
    await session.execute(text("DELETE FROM doc.filing"))
    await session.execute(text("DELETE FROM src.ingestion_run"))
    await session.commit()


async def _seed_kap_source(session: AsyncSession) -> None:
    """Wipe doc.* + insert 'kap' source. Read-only CLI tests start fresh."""
    await session.execute(text("DELETE FROM doc.filing_body"))
    await session.execute(text("DELETE FROM doc.filing_attachment"))
    await session.execute(text("DELETE FROM doc.filing"))
    await session.execute(text("DELETE FROM src.ingestion_run"))
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


async def _seed_one_filing(
    session: AsyncSession,
    fake: InMemoryFake,
    *,
    source_ref: str = "DOC-1",
    body: bytes = b"<html>cli-test body</html>",
    kind: str = "news",
    title: str = "Seeded for CLI test",
) -> str:
    """Seed exactly one kap filing via DocumentStore.put_filing. Returns filing_id."""
    from aslan_core.documents.client import DocumentStore

    run_id_row = await session.execute(
        text(
            "INSERT INTO src.ingestion_run (source_id, job_name, status) "
            "VALUES ('kap', 'cli_doc_seed', 'succeeded') RETURNING ingestion_run_id"
        )
    )
    run_id = cast(int, run_id_row.scalar_one())
    await session.commit()

    store = DocumentStore(session, object_client=fake, ingestion_run_id=run_id)
    result = await store.put_filing(
        source_id="kap",
        source_filing_ref=source_ref,
        entity_id=None,
        kind=kind,
        title=title,
        published_at=datetime(2026, 4, 28, 12, tzinfo=UTC),
        primary_bytes=body,
        primary_mime="text/html",
        primary_filename="main.html",
    )
    await session.commit()
    return str(result.filing.filing_id)


def _patch_factory(monkeypatch: pytest.MonkeyPatch, fake: InMemoryFake) -> None:
    """Replace the cli.doc module's object-client factory with a fake-returning lambda."""
    from aslan_core.cli import doc as doc_module

    monkeypatch.setattr(doc_module, "_object_client_factory", lambda: fake)


async def _invoke(cli: click.Command, args: list[str]) -> Result:
    """Run CliRunner.invoke in a worker thread so the CLI's ``asyncio.run``
    has its own event loop. The pytest-asyncio test owns the main loop;
    nesting ``asyncio.run`` inside it is forbidden."""
    runner = CliRunner()
    return await asyncio.to_thread(runner.invoke, cli, args)


# ─── Task 27 ─────────────────────────────────────────────────────────────


async def test_doc_get_returns_filing_json(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aslan_core.cli.main import cli

    await _seed_kap_source(session)
    filing_id = await _seed_one_filing(session, object_storage_fake)
    _patch_factory(monkeypatch, object_storage_fake)

    result = await _invoke(cli, ["doc", "get", filing_id, "--json"])
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    payload = json.loads(result.output)
    assert payload["filing_id"] == filing_id
    assert payload["source_id"] == "kap"
    assert payload["title"] == "Seeded for CLI test"


async def test_doc_get_human_output(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (no --json) prints a human-readable summary including filing_id."""
    from aslan_core.cli.main import cli

    await _seed_kap_source(session)
    filing_id = await _seed_one_filing(session, object_storage_fake)
    _patch_factory(monkeypatch, object_storage_fake)

    result = await _invoke(cli, ["doc", "get", filing_id])
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    assert filing_id in result.output


async def test_doc_get_unknown_filing_id_exits_nonzero(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown filing_id surfaces as a nonzero exit (typed DocumentNotFound under the hood)."""
    from aslan_core.cli.main import cli

    await _seed_kap_source(session)
    _patch_factory(monkeypatch, object_storage_fake)

    bogus = "00000000-0000-0000-0000-000000000000"
    result = await _invoke(cli, ["doc", "get", bogus, "--json"])
    assert result.exit_code != 0


async def test_doc_find_by_source_ref_returns_filing(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aslan_core.cli.main import cli

    await _seed_kap_source(session)
    filing_id = await _seed_one_filing(session, object_storage_fake, source_ref="FIND-ME")
    _patch_factory(monkeypatch, object_storage_fake)

    result = await _invoke(
        cli,
        ["doc", "find", "--source-id", "kap", "--source-ref", "FIND-ME", "--json"],
    )
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    payload = json.loads(result.output)
    assert payload["filing_id"] == filing_id
    assert payload["source_filing_ref"] == "FIND-ME"


async def test_doc_find_no_match_returns_null_in_json(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aslan_core.cli.main import cli

    await _seed_kap_source(session)
    _patch_factory(monkeypatch, object_storage_fake)

    result = await _invoke(
        cli,
        ["doc", "find", "--source-id", "kap", "--source-ref", "NOT-THERE", "--json"],
    )
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    payload = json.loads(result.output)
    assert payload is None


async def test_doc_chain_returns_list_of_revisions(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aslan_core.cli.main import cli

    await _seed_kap_source(session)
    filing_id = await _seed_one_filing(session, object_storage_fake, source_ref="CHAIN-CLI")
    _patch_factory(monkeypatch, object_storage_fake)

    result = await _invoke(cli, ["doc", "chain", filing_id, "--json"])
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["filing_id"] == filing_id
    assert payload[0]["revision_no"] == 1


# ─── Task 28: list / stats ────────────────────────────────────────────────


async def _seed_two_filings(session: AsyncSession, fake: InMemoryFake) -> tuple[str, str]:
    """Seed two distinct filings: one kap/news, one kap/material_event."""
    a = await _seed_one_filing(
        session,
        fake,
        source_ref="LIST-A",
        body=b"alpha-bytes",
        kind="news",
        title="Alpha title",
    )
    b = await _seed_one_filing(
        session,
        fake,
        source_ref="LIST-B",
        body=b"bravo-bytes",
        kind="material_event",
        title="Bravo title",
    )
    return a, b


async def test_doc_list_returns_all_rows(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aslan_core.cli.main import cli

    await _seed_kap_source(session)
    a, b = await _seed_two_filings(session, object_storage_fake)
    _patch_factory(monkeypatch, object_storage_fake)

    result = await _invoke(cli, ["doc", "list", "--json"])
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    ids = {row["filing_id"] for row in payload}
    assert {a, b} <= ids


async def test_doc_list_filters_by_kind(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aslan_core.cli.main import cli

    await _seed_kap_source(session)
    a, b = await _seed_two_filings(session, object_storage_fake)
    _patch_factory(monkeypatch, object_storage_fake)

    result = await _invoke(cli, ["doc", "list", "--kind", "news", "--json"])
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    payload = json.loads(result.output)
    ids = {row["filing_id"] for row in payload}
    assert a in ids
    assert b not in ids


async def test_doc_list_respects_limit(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aslan_core.cli.main import cli

    await _seed_kap_source(session)
    await _seed_two_filings(session, object_storage_fake)
    _patch_factory(monkeypatch, object_storage_fake)

    result = await _invoke(cli, ["doc", "list", "--limit", "1", "--json"])
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    payload = json.loads(result.output)
    assert len(payload) == 1


async def test_doc_stats_groups_by_source_and_kind(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aslan_core.cli.main import cli

    await _seed_kap_source(session)
    await _seed_two_filings(session, object_storage_fake)
    _patch_factory(monkeypatch, object_storage_fake)

    result = await _invoke(cli, ["doc", "stats", "--json"])
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    # Two rows: kap/news=1 + kap/material_event=1
    by_kind = {row["kind"]: row for row in payload if row["source_id"] == "kap"}
    assert by_kind["news"]["count"] == 1
    assert by_kind["material_event"]["count"] == 1
