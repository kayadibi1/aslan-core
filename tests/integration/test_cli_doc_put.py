from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

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


# ─── Task 30 ─────────────────────────────────────────────────────────────


async def test_doc_release_deletes_blob_and_row(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`aslan doc release <filing_id>` deletes every recorded bucket key
    (primary + xbrl + attachments) AND removes the doc.filing row."""
    from aslan_core.cli.main import cli

    await _seed_sources(session)
    _patch_factory(monkeypatch, object_storage_fake)

    primary = tmp_path / "manual.html"
    primary.write_bytes(b"<html>release-test</html>")

    put_result = await _invoke(
        cli,
        [
            "doc",
            "put",
            "--source-id",
            "manual",
            "--source-ref",
            "RELEASE-1",
            "--kind",
            "news",
            "--title",
            "Release test",
            "--published-at",
            "2026-04-28T12:00:00Z",
            "--primary-file",
            str(primary),
            "--json",
        ],
    )
    assert put_result.exit_code == 0, f"output={put_result.output}\nexc={put_result.exception!r}"
    payload = json.loads(put_result.output)
    filing_id = payload["filing_id"]
    assert len(object_storage_fake.all_keys()) == 1

    # Release.
    release_result = await _invoke(cli, ["doc", "release", filing_id])
    assert release_result.exit_code == 0, (
        f"output={release_result.output}\nexc={release_result.exception!r}"
    )

    # Bucket empty + row gone.
    assert object_storage_fake.all_keys() == set()
    await session.rollback()
    row_count = await session.scalar(
        text("SELECT COUNT(*) FROM doc.filing WHERE filing_id = :fid"),
        {"fid": UUID(filing_id)},
    )
    assert row_count == 0


async def test_doc_release_unknown_filing_id_exits_nonzero(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aslan_core.cli.main import cli

    await _seed_sources(session)
    _patch_factory(monkeypatch, object_storage_fake)

    bogus = "00000000-0000-0000-0000-000000000000"
    result = await _invoke(cli, ["doc", "release", bogus])
    assert result.exit_code != 0


async def test_doc_release_blocks_mid_chain_with_blobs_intact(
    session: AsyncSession,
    object_storage_fake: InMemoryFake,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex 2026-04-29: releasing a filing that a later revision
    references via previous_filing_id must abort BEFORE any bucket
    object is touched.

    The chain self-FK doc.filing.previous_filing_id has no ON DELETE
    CASCADE; without preflight, the CLI would delete blobs first, then
    fail on the row DELETE — leaving live rows pointing at missing
    blobs. Verifies: clear error message, non-zero exit, both v1 and
    v2 blobs and rows survive untouched.
    """
    from aslan_core.cli.main import cli

    await _seed_sources(session)
    _patch_factory(monkeypatch, object_storage_fake)

    # v1 — creates the chain head.
    v1_file = tmp_path / "v1.html"
    v1_file.write_bytes(b"<html>v1</html>")
    v1_result = await _invoke(
        cli,
        [
            "doc",
            "put",
            "--source-id",
            "manual",
            "--source-ref",
            "CHAIN-MID",
            "--kind",
            "news",
            "--title",
            "Chain v1",
            "--published-at",
            "2026-04-28T12:00:00Z",
            "--primary-file",
            str(v1_file),
            "--json",
        ],
    )
    assert v1_result.exit_code == 0, f"v1 put failed: {v1_result.output}"
    v1_id = UUID(json.loads(v1_result.output)["filing_id"])

    # v2 — different bytes, same source_ref → put_filing auto-detects v1
    # and links via previous_filing_id (spec §5.4 case C).
    v2_file = tmp_path / "v2.html"
    v2_file.write_bytes(b"<html>v2 different</html>")
    v2_result = await _invoke(
        cli,
        [
            "doc",
            "put",
            "--source-id",
            "manual",
            "--source-ref",
            "CHAIN-MID",
            "--kind",
            "news",
            "--title",
            "Chain v2",
            "--published-at",
            "2026-04-28T13:00:00Z",
            "--primary-file",
            str(v2_file),
            "--json",
        ],
    )
    assert v2_result.exit_code == 0, f"v2 put failed: {v2_result.output}"

    # Sanity: 2 filing rows + 2 blobs.
    keys_before = set(object_storage_fake.all_keys())
    assert len(keys_before) == 2
    await session.rollback()
    n_rows_before = await session.scalar(
        text("SELECT COUNT(*) FROM doc.filing WHERE source_filing_ref = 'CHAIN-MID'")
    )
    assert n_rows_before == 2

    # Try to release v1 (the mid-chain row). Must fail BEFORE touching
    # any blob.
    release_result = await _invoke(cli, ["doc", "release", str(v1_id)])
    assert release_result.exit_code != 0
    assert "previous_filing_id" in release_result.output

    # Both blobs still in the bucket.
    assert set(object_storage_fake.all_keys()) == keys_before

    # Both rows still in the DB.
    await session.rollback()
    n_rows_after = await session.scalar(
        text("SELECT COUNT(*) FROM doc.filing WHERE source_filing_ref = 'CHAIN-MID'")
    )
    assert n_rows_after == 2


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


class _FlakyFake(InMemoryFake):
    """Test helper: fail delete_object on any key matching `fail_substring`."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_substring: str | None = None

    async def delete_object(self, *, bucket: str, key: str) -> None:
        if self.fail_substring and self.fail_substring in key:
            raise RuntimeError(f"simulated S3 delete failure for {key}")
        await super().delete_object(bucket=bucket, key=key)


async def test_doc_release_aborts_db_delete_when_blob_delete_fails(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex 2026-04-29 (F4): if any blob delete fails, the DB row and
    attachment manifest must remain in place so the operator can rerun.

    Without this: row + manifest gone, orphan blobs unrecoverable from
    the operator's side. With this: rerun once the storage error
    clears — delete_object is S3-idempotent, already-deleted blobs
    no-op on retry, then the DB delete proceeds.
    """
    from aslan_core.cli.main import cli

    flaky = _FlakyFake()
    await _seed_sources(session)
    _patch_factory(monkeypatch, flaky)

    primary = tmp_path / "manual.html"
    primary.write_bytes(b"<html>flaky-release</html>")

    put_result = await _invoke(
        cli,
        [
            "doc",
            "put",
            "--source-id",
            "manual",
            "--source-ref",
            "FLAKY",
            "--kind",
            "news",
            "--title",
            "Flaky release",
            "--published-at",
            "2026-04-28T12:00:00Z",
            "--primary-file",
            str(primary),
            "--json",
        ],
    )
    assert put_result.exit_code == 0, f"put failed: {put_result.output}"
    filing_id = json.loads(put_result.output)["filing_id"]
    assert len(flaky.all_keys()) == 1

    # First release attempt — blob delete will fail.
    flaky.fail_substring = "primary"
    release_fail = await _invoke(cli, ["doc", "release", filing_id])
    assert release_fail.exit_code != 0
    assert "blob delete" in release_fail.output

    # The blob is still there; the DB row is still there. Operator can
    # rerun release once storage recovers.
    assert len(flaky.all_keys()) == 1
    await session.rollback()
    n_before = await session.scalar(
        text("SELECT COUNT(*) FROM doc.filing WHERE filing_id = :fid"),
        {"fid": UUID(filing_id)},
    )
    assert n_before == 1

    # Storage recovers — rerun. delete_object is idempotent, blob
    # delete proceeds, row delete commits.
    flaky.fail_substring = None
    release_ok = await _invoke(cli, ["doc", "release", filing_id])
    assert release_ok.exit_code == 0, f"retry failed: {release_ok.output}"

    assert flaky.all_keys() == set()
    await session.rollback()
    n_after = await session.scalar(
        text("SELECT COUNT(*) FROM doc.filing WHERE filing_id = :fid"),
        {"fid": UUID(filing_id)},
    )
    assert n_after == 0
