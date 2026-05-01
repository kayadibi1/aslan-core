"""Sentinel matrix — Task 13.

Spec §6.3 + §8.2 sentinel suite: for every (page, forbidden_field)
pair, seed a unique sentinel into the forbidden column under a
privileged role, GET the page through the dashboard app, assert
the sentinel does NOT appear in the response body. The forbidden
projection contract (queries.py never SELECTs the column) is the
proximate guarantee; the column-allowlist GRANT migration 0020
installed is the load-bearing privilege-layer floor — covered
separately in ``test_dashboard_forbidden_columns_are_revoked.py``.

Each test case has a distinct seed shape (different FK chains,
different column types, different surrogate-key strategies), so
the matrix is expressed as one ``async def`` per (page, field)
rather than a flat parametrize. Keeping the seeds explicit makes
a future GDPR review easier — every piece of forbidden-data
plumbing is one file away from its assertion.

Note on missing columns: ``doc.filing.summary_text`` and
``doc.filing_attachment.body_blob`` are listed in spec §6.3 but
do not exist in the v0.6.0 schema (filing_body holds body_text
exclusively; attachments live in S3 referenced by ``object_key``).
The matrix covers what's reachable; if a future schema migration
adds those columns, append a test here per the same shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.dashboard.app import app, configure_app

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


# ── Seeding helpers ──────────────────────────────────────────────


async def _ensure_kap_source(session: AsyncSession) -> None:
    """Most seeds chain through src.source('kap') + an ingestion
    run, so we share the bootstrap helper."""
    await session.execute(
        text(
            "INSERT INTO src.source(source_id, name, kind, license_status) "
            "VALUES('kap', 'KAP', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING"
        )
    )


async def _new_ingestion_run(session: AsyncSession) -> int:
    await _ensure_kap_source(session)
    return int(
        (
            await session.execute(
                text(
                    "INSERT INTO src.ingestion_run(source_id, job_name, status) "
                    "VALUES('kap', 'sentinel-matrix', 'succeeded') "
                    "RETURNING ingestion_run_id"
                )
            )
        ).scalar_one()
    )


async def _hit(path: str) -> str:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"
    body: str = response.text
    return body


# ── /outbox ──────────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_does_not_leak_payload(
    _configured_dashboard: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sentinel = "sentinel-payload-outbox-04ff"
    run_id = await _new_ingestion_run(session)
    await session.execute(
        text(
            "INSERT INTO streams.outbox "
            "(stream_name, event_id, schema_version, payload, "
            " producer_run_id, source_id) "
            "VALUES('kap', gen_random_uuid(), 1, "
            "       jsonb_build_object('secret', CAST(:sentinel AS text)), :run, 'kap')"
        ),
        {"sentinel": sentinel, "run": run_id},
    )
    await session.commit()
    try:
        body = await _hit("/outbox")
        assert sentinel not in body
    finally:
        async with session_factory() as s:
            # Delete outbox before ingestion_run to avoid the
            # outbox_producer_run_id_fkey CASCADE; the autouse
            # _wipe_streams_tables_after also wipes outbox, but it
            # runs AFTER this finally block.
            await s.execute(
                text("DELETE FROM streams.outbox WHERE producer_run_id = :rid"),
                {"rid": run_id},
            )
            await s.execute(
                text("DELETE FROM src.ingestion_run WHERE ingestion_run_id = :rid"),
                {"rid": run_id},
            )
            await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_outbox_does_not_leak_last_error(
    _configured_dashboard: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sentinel = "sentinel-last-error-outbox-traceback-9d12"
    run_id = await _new_ingestion_run(session)
    await session.execute(
        text(
            "INSERT INTO streams.outbox "
            "(stream_name, event_id, schema_version, payload, "
            " producer_run_id, source_id, last_error) "
            "VALUES('kap', gen_random_uuid(), 1, '{}'::jsonb, "
            "       :run, 'kap', :sentinel)"
        ),
        {"sentinel": sentinel, "run": run_id},
    )
    await session.commit()
    try:
        body = await _hit("/outbox")
        assert sentinel not in body
    finally:
        async with session_factory() as s:
            # Delete outbox before ingestion_run to avoid the
            # outbox_producer_run_id_fkey CASCADE; the autouse
            # _wipe_streams_tables_after also wipes outbox, but it
            # runs AFTER this finally block.
            await s.execute(
                text("DELETE FROM streams.outbox WHERE producer_run_id = :rid"),
                {"rid": run_id},
            )
            await s.execute(
                text("DELETE FROM src.ingestion_run WHERE ingestion_run_id = :rid"),
                {"rid": run_id},
            )
            await s.commit()


# ── /deadletter ──────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_does_not_leak_payload_excerpt(
    _configured_dashboard: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sentinel = "sentinel-deadletter-payload-excerpt-c4a1"
    failure_id: int = (
        await session.execute(
            text(
                "INSERT INTO streams.deadletter_log "
                "(stream_name, deadletter_stream, event_id, original_message_id, "
                " group_name, consumer_name, failure_count, last_error, "
                " payload_excerpt) "
                "VALUES('kap', 'kap__deadletter', gen_random_uuid(), '0-0', "
                "       'g', 'c', 1, 'err', "
                "       jsonb_build_object('leak', CAST(:sentinel AS text))) "
                "RETURNING failure_id"
            ),
            {"sentinel": sentinel},
        )
    ).scalar_one()
    await session.commit()
    try:
        body = await _hit("/deadletter")
        assert sentinel not in body
    finally:
        async with session_factory() as s:
            await s.execute(
                text("DELETE FROM streams.deadletter_log WHERE failure_id = :fid"),
                {"fid": failure_id},
            )
            await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_deadletter_does_not_leak_last_error(
    _configured_dashboard: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sentinel = "sentinel-deadletter-traceback-b88a"
    failure_id: int = (
        await session.execute(
            text(
                "INSERT INTO streams.deadletter_log "
                "(stream_name, deadletter_stream, event_id, original_message_id, "
                " group_name, consumer_name, failure_count, last_error) "
                "VALUES('kap', 'kap__deadletter', gen_random_uuid(), '0-0', "
                "       'g', 'c', 1, :sentinel) "
                "RETURNING failure_id"
            ),
            {"sentinel": f"ValueError: {sentinel}\n  Traceback..."},
        )
    ).scalar_one()
    await session.commit()
    try:
        body = await _hit("/deadletter")
        assert sentinel not in body
        # The leading exception class name is fine to render — the
        # spec explicitly carves out last_error_kind as the projection.
        # We don't assert "ValueError" absent here because that token
        # is the *intentional* projection in v0.7.x; in v0.6.0
        # last_error_kind is hard-coded to None so the substring won't
        # be there either, but the looser assertion keeps the test
        # forward-compatible.
    finally:
        async with session_factory() as s:
            await s.execute(
                text("DELETE FROM streams.deadletter_log WHERE failure_id = :fid"),
                {"fid": failure_id},
            )
            await s.commit()


# ── /ingestion ───────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_ingestion_does_not_leak_error_text(
    _configured_dashboard: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sentinel = "sentinel-ingestion-error-2eb4"
    await _ensure_kap_source(session)
    run_id: int = (
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run(source_id, job_name, status, error) "
                "VALUES('kap', 'sentinel-matrix-ingestion', 'failed', :err) "
                "RETURNING ingestion_run_id"
            ),
            {"err": f"RuntimeError: {sentinel}\n  long traceback follows..."},
        )
    ).scalar_one()
    await session.commit()
    try:
        body = await _hit("/ingestion")
        assert sentinel not in body
    finally:
        async with session_factory() as s:
            await s.execute(
                text("DELETE FROM src.ingestion_run WHERE ingestion_run_id = :rid"),
                {"rid": run_id},
            )
            await s.commit()


# ── /documents ───────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_documents_does_not_leak_body_text(
    _configured_dashboard: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sentinel = "sentinel-filing-body-text-7ab2"
    run_id = await _new_ingestion_run(session)
    filing_id_str: str = str(
        (
            await session.execute(
                text(
                    "INSERT INTO doc.filing "
                    "(source_id, source_filing_ref, kind, title, language, "
                    " published_at, primary_object_key, primary_mime, "
                    " primary_sha256, primary_bytes, ingestion_run_id, "
                    " revision_no) "
                    "VALUES('kap', 'sentinel-ref', 'announcement', "
                    "       'sentinel filing', 'en', now(), "
                    "       's3://k/v', 'application/pdf', "
                    "       repeat('a', 64), 1, :rid, 1) "
                    "RETURNING filing_id"
                ),
                {"rid": run_id},
            )
        ).scalar_one()
    )
    await session.execute(
        text(
            "INSERT INTO doc.filing_body(filing_id, body_text, body_lang) "
            "VALUES(CAST(:fid AS uuid), :body, 'en')"
        ),
        {"fid": filing_id_str, "body": f"This is a long body containing the secret {sentinel}."},
    )
    await session.commit()
    try:
        body = await _hit("/documents")
        assert sentinel not in body
    finally:
        async with session_factory() as s:
            await s.execute(
                text("DELETE FROM doc.filing_body WHERE filing_id = CAST(:fid AS uuid)"),
                {"fid": filing_id_str},
            )
            await s.execute(
                text("DELETE FROM doc.filing WHERE filing_id = CAST(:fid AS uuid)"),
                {"fid": filing_id_str},
            )
            await s.execute(
                text("DELETE FROM src.ingestion_run WHERE ingestion_run_id = :rid"),
                {"rid": run_id},
            )
            await s.commit()


# ── /redactions ──────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_redactions_does_not_leak_redacted_payload(
    _configured_dashboard: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sentinel = "sentinel-redacted-payload-f3c7"
    event_id_str: str = str(
        (
            await session.execute(
                text(
                    "INSERT INTO streams.redaction_registry "
                    "(event_id, redaction_reason, original_stream, "
                    " redacted_payload, redacted_payload_hash, "
                    " original_payload_hash) "
                    "VALUES(gen_random_uuid(), 'art-17', 'kap', "
                    "       jsonb_build_object('secret', CAST(:sentinel AS text)), "
                    "       repeat('a', 64), repeat('b', 64)) "
                    "RETURNING event_id"
                ),
                {"sentinel": sentinel},
            )
        ).scalar_one()
    )
    await session.commit()
    try:
        body = await _hit("/redactions")
        assert sentinel not in body
    finally:
        async with session_factory() as s:
            await s.execute(
                text("DELETE FROM streams.redaction_registry WHERE event_id = CAST(:evt AS uuid)"),
                {"evt": event_id_str},
            )
            await s.commit()


# ── /audit ───────────────────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_does_not_leak_metadata(
    _configured_dashboard: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Spec §4.8: audit.events.metadata is forbidden; the page
    projects only a key count via the SECURITY DEFINER helper."""
    sentinel = "sentinel-audit-metadata-9f83"
    event_id: int = (
        await session.execute(
            text(
                "INSERT INTO audit.events "
                "(occurred_at, actor_id, actor_kind, operation, "
                " target_schema, target_table, target_pk, metadata) "
                "VALUES(now(), 'cli:test@host', 'user', 'sentinel-test', "
                "       'audit', 'events', '{}'::jsonb, "
                "       jsonb_build_object('leak', CAST(:sentinel AS text))) "
                "RETURNING event_id"
            ),
            {"sentinel": sentinel},
        )
    ).scalar_one()
    await session.commit()
    try:
        body = await _hit("/audit")
        assert sentinel not in body
    finally:
        async with session_factory() as s:
            await s.execute(
                text("DELETE FROM audit.events WHERE event_id = :eid"),
                {"eid": event_id},
            )
            await s.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_truncates_client_ip(
    _configured_dashboard: None,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Spec §4.8 + §8.2: client_ip is truncated to /24 (v4) or /48
    (v6). Seed an audit row with a documentation-range IP, render
    the page, assert the truncated CIDR is present and the full IP
    is absent. The truncation is performed by the SECURITY DEFINER
    helper ``audit.event_client_ip_truncated`` (migration 0022)."""
    full_ip = "198.51.100.42"
    truncated = "198.51.100.0/24"
    event_id: int = (
        await session.execute(
            text(
                "INSERT INTO audit.events "
                "(occurred_at, actor_id, actor_kind, operation, "
                " target_schema, target_table, target_pk, client_ip) "
                "VALUES(now(), 'cli:test@host', 'user', 'truncate-test', "
                "       'audit', 'events', '{}'::jsonb, CAST(:ip AS inet)) "
                "RETURNING event_id"
            ),
            {"ip": full_ip},
        )
    ).scalar_one()
    await session.commit()
    try:
        body = await _hit("/audit")
        assert truncated in body, f"truncated CIDR {truncated!r} missing"
        assert full_ip not in body, f"full client_ip {full_ip!r} leaked"
    finally:
        async with session_factory() as s:
            await s.execute(
                text("DELETE FROM audit.events WHERE event_id = :eid"),
                {"eid": event_id},
            )
            await s.commit()
