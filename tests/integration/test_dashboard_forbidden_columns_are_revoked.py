"""GDPR boundary at the SQL privilege layer: every column in spec
§6.3's forbidden list raises ``InsufficientPrivilegeError`` under the
dashboard role, even when seeded with sentinel data.

Spec §8.2 + codex round-7: deny-by-default Pydantic VMs in Python are
defense in depth, but the load-bearing guarantee is at the privilege
layer — column-allowlist GRANT means a SQL injection or in-process
``connection.exec_driver_sql`` cannot read the forbidden bytes either.

Seeded rows confirm the error is the privilege check, not an empty
table. Each row carries a unique sentinel string in the forbidden
column; the assertion is that the SELECT raises before any row scan
returns the sentinel. (PG runs the privilege check at parse-analyze,
strictly before tuple access.)

The ``SELECT * FROM <table>`` cases prove that ``SELECT *`` cannot be
used to bypass the column-allowlist — ``*`` expands to every column at
parse time and at least one is unlisted, so the same 42501 fires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


_SENTINEL = "FORBIDDEN-COLUMN-SENTINEL-aab3"


@pytest_asyncio.fixture(loop_scope="session")
async def _seed_forbidden_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[dict[str, str]]:
    """Seed one row per table with sentinel values in every forbidden
    column. Commits so the dashboard connection sees the rows. Teardown
    deletes the seeded rows in FK-safe order.

    Returns a dict of identifiers the tests can use if they want to
    verify they targeted the right row (currently unused — the assertion
    is privilege failure, not row content).
    """
    eid_outbox = uuid4()
    eid_dl = uuid4()
    eid_redact = uuid4()
    filing_id = uuid4()
    audit_event_pk: tuple[int, str] | None = None

    async with session_factory() as s:
        # src.source + src.ingestion_run (FK target for outbox + filing)
        await s.execute(
            text(
                "INSERT INTO src.source (source_id, name, kind, license_status) "
                "VALUES ('forbid-test', 'forbid-test', 'manual', 'open') "
                "ON CONFLICT DO NOTHING"
            )
        )
        run_id: int = (
            await s.execute(
                text(
                    "INSERT INTO src.ingestion_run "
                    "  (source_id, job_name, status, error) "
                    "VALUES ('forbid-test', 'forbid-test', 'failed', :err) "
                    "RETURNING ingestion_run_id"
                ),
                {"err": _SENTINEL},
            )
        ).scalar_one()

        # streams.outbox — payload + last_error are forbidden.
        # CAST(:x AS jsonb) is used in place of :x::jsonb because
        # SQLAlchemy's text()-bind scanner does not match a bind whose
        # word is immediately followed by `::`.
        await s.execute(
            text(
                "INSERT INTO streams.outbox "
                "(stream_name, event_id, schema_version, payload, "
                " producer_run_id, source_id, last_error) "
                "VALUES ('kap', :eid, 1, CAST(:payload AS jsonb), :run, "
                "        'forbid-test', :err)"
            ),
            {
                "eid": eid_outbox,
                "payload": f'{{"sentinel": "{_SENTINEL}"}}',
                "run": run_id,
                "err": _SENTINEL,
            },
        )

        # streams.deadletter_log — payload_excerpt + last_error are forbidden.
        # ``deadletter_routing_uq`` requires uniqueness on
        # ``(stream_name, group_name, original_message_id)``; use a unique
        # message id per fixture run to avoid colliding with rows other
        # tests have committed and not cleaned up.
        unique_msg_id = f"forbid-{eid_dl.hex[:12]}"
        await s.execute(
            text(
                "INSERT INTO streams.deadletter_log "
                "(stream_name, deadletter_stream, event_id, original_message_id, "
                " group_name, consumer_name, failure_count, last_error, "
                " payload_excerpt) "
                "VALUES ('kap', 'kap.dead', :eid, :msg_id, 'g-forbid', 'c', 1, :err, "
                "        CAST(:pe AS jsonb))"
            ),
            {
                "eid": eid_dl,
                "msg_id": unique_msg_id,
                "err": _SENTINEL,
                "pe": f'{{"sentinel": "{_SENTINEL}"}}',
            },
        )

        # streams.redaction_registry — redacted_payload is forbidden
        await s.execute(
            text(
                "INSERT INTO streams.redaction_registry "
                "(event_id, redaction_reason, original_stream, redacted_payload, "
                " redacted_payload_hash, original_payload_hash) "
                "VALUES (:eid, 'Art.17', 'kap', CAST(:rp AS jsonb), "
                "        repeat('a', 64), repeat('b', 64))"
            ),
            {
                "eid": eid_redact,
                "rp": f'{{"sentinel": "{_SENTINEL}"}}',
            },
        )

        # doc.filing — title is forbidden
        await s.execute(
            text(
                "INSERT INTO doc.filing "
                "(filing_id, source_id, source_filing_ref, kind, title, "
                " published_at, primary_object_key, primary_mime, primary_sha256, "
                " primary_bytes, ingestion_run_id, revision_no) "
                "VALUES (:fid, 'forbid-test', 'ref-1', 'kind', :title, "
                "        now(), 'k/1', 'application/pdf', repeat('c', 64), "
                "        100, :run, 1)"
            ),
            {"fid": filing_id, "title": _SENTINEL, "run": run_id},
        )

        # doc.filing_body — body_text is forbidden
        await s.execute(
            text(
                "INSERT INTO doc.filing_body "
                "(filing_id, body_text, body_lang) "
                "VALUES (:fid, :body, 'tr')"
            ),
            {"fid": filing_id, "body": _SENTINEL},
        )

        # audit.events — before, after, metadata are forbidden
        result = await s.execute(
            text(
                "INSERT INTO audit.events "
                "(actor_id, actor_kind, operation, target_schema, target_table, "
                " target_pk, before, after, metadata) "
                "VALUES ('user-forbid', 'user', 'insert', 'streams', 'outbox', "
                "        CAST(:pk AS jsonb), CAST(:bef AS jsonb), "
                "        CAST(:aft AS jsonb), CAST(:md AS jsonb)) "
                "RETURNING event_id, occurred_at"
            ),
            {
                "pk": '{"x":1}',
                "bef": f'{{"sentinel": "{_SENTINEL}-before"}}',
                "aft": f'{{"sentinel": "{_SENTINEL}-after"}}',
                "md": f'{{"sentinel": "{_SENTINEL}-md"}}',
            },
        )
        ev_id, ev_ts = result.one()
        audit_event_pk = (ev_id, ev_ts.isoformat())

        await s.commit()

    yield {
        "outbox_event_id": str(eid_outbox),
        "deadletter_event_id": str(eid_dl),
        "redact_event_id": str(eid_redact),
        "filing_id": str(filing_id),
        "audit_event_id": str(audit_event_pk[0]) if audit_event_pk else "",
    }

    # Teardown: delete in FK-safe order. The conftest autouse
    # ``_wipe_streams_tables_after`` runs AFTER this fixture's teardown
    # (autouse fixtures from conftest tear down LAST), so this fixture
    # must clean ``streams.outbox`` and ``streams.event_id_to_redis``
    # itself — otherwise the ``src.ingestion_run`` delete fails with
    # ``outbox_producer_run_id_fkey`` and the whole teardown rolls back,
    # leaving every other inserted row in place to collide with the
    # next parametrize case.
    async with session_factory() as s:
        await s.execute(text("DELETE FROM streams.event_id_to_redis"))
        await s.execute(text("DELETE FROM streams.outbox"))
        await s.execute(
            text("DELETE FROM doc.filing_body WHERE filing_id = :fid"),
            {"fid": filing_id},
        )
        await s.execute(
            text("DELETE FROM doc.filing WHERE filing_id = :fid"),
            {"fid": filing_id},
        )
        await s.execute(
            text("DELETE FROM streams.redaction_registry WHERE event_id = :eid"),
            {"eid": eid_redact},
        )
        await s.execute(
            text("DELETE FROM streams.deadletter_log WHERE event_id = :eid"),
            {"eid": eid_dl},
        )
        await s.execute(
            text("DELETE FROM src.ingestion_run WHERE ingestion_run_id = :rid"),
            {"rid": run_id},
        )
        await s.execute(text("DELETE FROM src.source WHERE source_id = 'forbid-test'"))
        if audit_event_pk is not None:
            await s.execute(
                text("DELETE FROM audit.events WHERE event_id = :eid AND occurred_at = :ts"),
                {"eid": audit_event_pk[0], "ts": ev_ts},
            )
        await s.commit()


# ── (table, column) pairs from spec §6.3 forbidden list ──

_FORBIDDEN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("streams.outbox", "payload"),
    ("streams.outbox", "last_error"),
    ("streams.deadletter_log", "payload_excerpt"),
    ("streams.deadletter_log", "last_error"),
    ("streams.redaction_registry", "redacted_payload"),
    ("src.ingestion_run", "error"),
    ("doc.filing", "title"),
    ("doc.filing_body", "body_text"),
    ("audit.events", "before"),
    ("audit.events", "after"),
    ("audit.events", "metadata"),
    # Codex branch-state F-1: raw client_ip + user_agent are no
    # longer in the dashboard role's column-allowlist (migration
    # 0022). The truncated CIDR is exposed via the SECURITY DEFINER
    # helper ``audit.event_client_ip_truncated`` instead.
    ("streams.outbox", "client_ip"),
    ("streams.outbox", "user_agent"),
    ("doc.filing", "client_ip"),
    ("doc.filing", "user_agent"),
    ("doc.filing_body", "client_ip"),
    ("doc.filing_body", "user_agent"),
    ("audit.events", "client_ip"),
    ("audit.events", "user_agent"),
)


@pytest.mark.parametrize(("table", "column"), _FORBIDDEN_COLUMNS)
@pytest.mark.asyncio(loop_scope="session")
async def test_forbidden_column_select_raises_insufficient_privilege(
    aslan_dashboard_conn: asyncpg.Connection,
    _seed_forbidden_rows: dict[str, str],
    table: str,
    column: str,
) -> None:
    # ``table`` and ``column`` come from the hardcoded parametrize tuple
    # — no untrusted input — so f-string is safe (and asyncpg can't bind
    # identifiers anyway).
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await aslan_dashboard_conn.fetch(f"SELECT {column} FROM {table} LIMIT 1")  # noqa: S608


# ── Tables with at least one forbidden column: SELECT * also fails ──

_TABLES_WITH_FORBIDDEN_COLUMNS: tuple[str, ...] = (
    "streams.outbox",
    "streams.deadletter_log",
    "streams.redaction_registry",
    "src.ingestion_run",
    "doc.filing",
    "doc.filing_body",
    "audit.events",
)


@pytest.mark.parametrize("table", _TABLES_WITH_FORBIDDEN_COLUMNS)
@pytest.mark.asyncio(loop_scope="session")
async def test_select_star_on_table_with_forbidden_column_fails(
    aslan_dashboard_conn: asyncpg.Connection,
    _seed_forbidden_rows: dict[str, str],
    table: str,
) -> None:
    """``SELECT *`` expands to every column at parse time. At least one
    column is unlisted in the column-allowlist GRANT, so 42501 fires —
    the same as the explicit-column case. Closes the ``SELECT *`` bypass."""
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await aslan_dashboard_conn.fetch(f"SELECT * FROM {table} LIMIT 1")  # noqa: S608
