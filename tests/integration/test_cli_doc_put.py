from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

import click
import pytest
from click.testing import CliRunner, Result
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.documents.object_storage import InMemoryFake

pytestmark = pytest.mark.integration


async def _seed_sources(session: AsyncSession) -> None:
    """Wipe doc.* + ensure 'manual' source exists. doc put writes attribution
    to src.ingestion_run, which has an FK to src.source."""
    await session.execute(text("DELETE FROM doc.filing_body"))
    await session.execute(text("DELETE FROM doc.filing_attachment"))
    await session.execute(text("DELETE FROM doc.filing"))
    await session.execute(text("DELETE FROM src.ingestion_run"))
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('manual', 'Manual', 'manual', 'open') ON CONFLICT DO NOTHING"
        )
    )
    await session.commit()


def _patch_factory(monkeypatch: pytest.MonkeyPatch, fake: InMemoryFake) -> None:
    from aslan_core.cli import doc as doc_module

    monkeypatch.setattr(doc_module, "_object_client_factory", lambda: fake)


async def _invoke(cli: click.Command, args: list[str]) -> Result:
    runner = CliRunner()
    return await asyncio.to_thread(runner.invoke, cli, args)


# ─── Task 29 ─────────────────────────────────────────────────────────────


async def test_doc_put_inserts_filing_and_uploads_blob(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`aslan doc put` reads the local file, uploads to (fake) S3, inserts
    the doc.filing row, and prints the new filing_id (JSON mode)."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)
    _patch_factory(monkeypatch, object_storage_fake)

    primary = tmp_path / "manual.html"
    primary.write_bytes(b"<html>manual put body</html>")

    result = await _invoke(
        cli,
        [
            "doc",
            "put",
            "--source-id",
            "manual",
            "--source-ref",
            "MANUAL-1",
            "--kind",
            "news",
            "--title",
            "Manual put test",
            "--published-at",
            "2026-04-28T12:00:00Z",
            "--primary-file",
            str(primary),
            "--primary-mime",
            "text/html",
            "--json",
        ],
    )
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    payload = json.loads(result.output)
    assert "filing_id" in payload
    filing_id = UUID(payload["filing_id"])
    assert payload.get("created") is True
    assert payload["bucket"] == "aslan-filings"
    assert isinstance(payload.get("object_keys"), list)
    assert len(payload["object_keys"]) == 1

    # The bucket has the blob and the body matches.
    assert len(object_storage_fake.all_keys()) == 1
    bucket, key = next(iter(object_storage_fake.all_keys()))
    assert object_storage_fake.get_body(bucket, key) == b"<html>manual put body</html>"

    # The DB row exists; rollback the test session first because the CLI
    # invocation committed via its own session, but our test session has
    # an open snapshot from before the commit.
    await session.rollback()
    row_count = await session.scalar(
        text("SELECT COUNT(*) FROM doc.filing WHERE filing_id = :fid"),
        {"fid": filing_id},
    )
    assert row_count == 1


async def test_doc_put_infers_mime_when_not_passed(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`--primary-mime` is optional; mimetypes.guess_type fills it in from
    the filename extension."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)
    _patch_factory(monkeypatch, object_storage_fake)

    primary = tmp_path / "manual.pdf"
    primary.write_bytes(b"%PDF-1.4 fake pdf")

    result = await _invoke(
        cli,
        [
            "doc",
            "put",
            "--source-id",
            "manual",
            "--source-ref",
            "MANUAL-PDF",
            "--kind",
            "news",
            "--title",
            "PDF",
            "--published-at",
            "2026-04-28T12:00:00Z",
            "--primary-file",
            str(primary),
            "--json",
        ],
    )
    assert result.exit_code == 0, f"output={result.output}\nexc={result.exception!r}"
    payload = json.loads(result.output)
    filing_id = UUID(payload["filing_id"])

    await session.rollback()
    mime = await session.scalar(
        text("SELECT primary_mime FROM doc.filing WHERE filing_id = :fid"),
        {"fid": filing_id},
    )
    assert mime == "application/pdf"
