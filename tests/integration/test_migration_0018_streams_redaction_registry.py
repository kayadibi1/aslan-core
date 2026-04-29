"""Migration 0018 — streams.redaction_registry + aslan_app role +
SECURITY DEFINER function (v0.5.0 Task 5b)."""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


def _async_dsn_to_asyncpg(dsn: str) -> str:
    """Strip the ``+asyncpg`` driver suffix so ``asyncpg.connect`` accepts it."""
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.mark.asyncio(loop_scope="session")
async def test_redaction_registry_columns(engine: AsyncEngine) -> None:
    """Codex F6 round 2: redacted_payload + original_payload_hash NOT
    NULL — the registry persists the FULL redacted payload."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema='streams' "
                    "AND table_name='redaction_registry'"
                )
            )
        ).all()
    cols: dict[str, tuple[str, str]] = {r[0]: (r[1], r[2]) for r in rows}
    assert cols["event_id"] == ("uuid", "NO")
    assert cols["redaction_reason"] == ("text", "NO")
    assert cols["redacted_at"] == ("timestamp with time zone", "NO")
    assert cols["original_stream"] == ("text", "NO")
    assert cols["redacted_payload"] == ("jsonb", "NO")
    assert cols["redacted_payload_hash"] == ("text", "NO")
    assert cols["original_payload_hash"] in {("character", "NO"), ("text", "NO")}


@pytest.mark.asyncio(loop_scope="session")
async def test_aslan_app_role_exists(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        n = (
            await conn.execute(text("SELECT count(*) FROM pg_roles WHERE rolname='aslan_app'"))
        ).scalar_one()
    assert n == 1, "aslan_app role must exist (idempotent CREATE)"


@pytest.mark.asyncio(loop_scope="session")
async def test_redaction_registry_insert_function_exists(engine: AsyncEngine) -> None:
    """Codex F15 round 6: SECURITY DEFINER function is the only writable surface."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT prosecdef FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='streams' AND p.proname='redaction_registry_insert'"
            )
        )
        sec_defs = [r[0] for r in result.all()]
    assert sec_defs and all(sec_defs), "function must exist + be SECURITY DEFINER"


@pytest.mark.asyncio(loop_scope="session")
async def test_redaction_registry_insert_revoked_for_app_role(pg_dsn: str) -> None:
    """Codex F15 round 6: direct INSERT/UPDATE/DELETE on the registry is
    REVOKEd from aslan_app. Connect AS aslan_app (no login, but the test
    impersonates via SET ROLE) and assert direct INSERT raises."""
    conn = await asyncpg.connect(dsn=_async_dsn_to_asyncpg(pg_dsn))
    try:
        await conn.execute("SET ROLE aslan_app")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO streams.redaction_registry "
                "(event_id, redaction_reason, original_stream, "
                " redacted_payload, redacted_payload_hash, "
                " original_payload_hash) "
                "VALUES (gen_random_uuid(), 'Art.17', 's', "
                "        '{}'::jsonb, repeat('a', 64), repeat('b', 64))"
            )
    finally:
        await conn.execute("RESET ROLE")
        await conn.close()


@pytest.mark.asyncio(loop_scope="session")
async def test_redaction_registry_insert_function_executable_for_app_role(
    pg_dsn: str,
) -> None:
    """Codex F15 round 6: GRANT EXECUTE on the function to aslan_app —
    the only mutation path."""
    conn = await asyncpg.connect(dsn=_async_dsn_to_asyncpg(pg_dsn))
    try:
        await conn.execute("SET ROLE aslan_app")
        await conn.execute(
            "SELECT streams.redaction_registry_insert("
            "  gen_random_uuid(), 'Art.17', now(), 's', "
            "  '{}'::jsonb, repeat('a', 64), repeat('b', 64))"
        )
    finally:
        await conn.execute("RESET ROLE")
        await conn.close()


@pytest.mark.asyncio(loop_scope="session")
async def test_redaction_registry_insert_takes_advisory_lock_first(
    pg_dsn: str,
) -> None:
    """Codex F15 round 6 — function correctness: in session A, BEGIN +
    call the function; in session B, attempt the SAME advisory lock and
    assert B blocks until A commits."""
    eid = str(uuid4())
    raw_dsn = _async_dsn_to_asyncpg(pg_dsn)
    a = await asyncpg.connect(dsn=raw_dsn)
    b = await asyncpg.connect(dsn=raw_dsn)
    try:
        await a.execute("BEGIN")
        await a.execute(
            "SELECT streams.redaction_registry_insert("
            "  $1::uuid, 'Art.17', now(), 's', "
            "  '{}'::jsonb, repeat('a', 64), repeat('b', 64))",
            eid,
        )

        async def _try_lock() -> bool:
            res: bool = await b.fetchval(
                "SELECT pg_try_advisory_xact_lock("
                "  hashtextextended('streams.redaction:' || $1, 0))",
                eid,
            )
            return res

        await b.execute("BEGIN")
        got = await _try_lock()
        assert got is False, "B must NOT acquire the lock while A holds it"
        await b.execute("ROLLBACK")
        await a.execute("COMMIT")

        await b.execute("BEGIN")
        got_after = await _try_lock()
        assert got_after is True, "B must acquire the lock once A commits"
        await b.execute("ROLLBACK")
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio(loop_scope="session")
async def test_redaction_registry_pk_is_event_id(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        pk_rows = (
            await conn.execute(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid=i.indrelid "
                    "AND a.attnum=ANY(i.indkey) "
                    "WHERE i.indrelid='streams.redaction_registry'::regclass "
                    "AND i.indisprimary"
                )
            )
        ).all()
    assert {r[0] for r in pk_rows} == {"event_id"}


@pytest.mark.asyncio(loop_scope="session")
async def test_aslan_app_can_select_redaction_registry(pg_dsn: str) -> None:
    """SELECT is GRANTed; only mutation is REVOKEd."""
    conn = await asyncpg.connect(dsn=_async_dsn_to_asyncpg(pg_dsn))
    try:
        await conn.execute("SET ROLE aslan_app")
        # No exception means GRANT SELECT worked.
        await conn.fetchval("SELECT count(*) FROM streams.redaction_registry")
    finally:
        await conn.execute("RESET ROLE")
        await conn.close()
