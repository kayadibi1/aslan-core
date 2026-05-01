"""Hard boundary: writes are blocked at the GRANT layer even when an
attacker disables the read-only GUC.

Spec §5.1 + §8.2: ``aslan_dashboard`` has the soft floor
``default_transaction_read_only = on`` AND the hard floor of
column-allowlist + revoked-DML GRANTs. Setting the GUC off explicitly
removes the soft floor; the hard floor MUST still reject every
mutation. This is the contract that holds even if a future bug ships
a session that opens with ``default_transaction_read_only=off``.

Each operation below targets a table the migration grants only SELECT
on (or, for ``streams.redaction_registry_insert``, a SECURITY DEFINER
the dashboard role has been REVOKEd from).
"""

from __future__ import annotations

import asyncpg
import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_insert_into_audit_events_denied_with_guc_off(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    await aslan_dashboard_conn.execute("SET default_transaction_read_only = off")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await aslan_dashboard_conn.execute(
            "INSERT INTO audit.events "
            "(actor_id, actor_kind, operation, target_schema, target_table, target_pk) "
            "VALUES ('attacker', 'user', 'forge', 'streams', 'outbox', '{}'::jsonb)"
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_insert_into_streams_outbox_denied_with_guc_off(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    await aslan_dashboard_conn.execute("SET default_transaction_read_only = off")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await aslan_dashboard_conn.execute(
            "INSERT INTO streams.outbox (stream_name, event_id, schema_version, "
            "payload, producer_run_id, source_id) "
            "VALUES ('kap', gen_random_uuid(), 1, '{}'::jsonb, 1, 'kap')"
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_update_doc_filing_denied_with_guc_off(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    await aslan_dashboard_conn.execute("SET default_transaction_read_only = off")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await aslan_dashboard_conn.execute(
            "UPDATE doc.filing SET source_url = 'https://attacker.example' "
            "WHERE filing_id = gen_random_uuid()"
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_delete_from_ts_observation_denied_with_guc_off(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    await aslan_dashboard_conn.execute("SET default_transaction_read_only = off")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await aslan_dashboard_conn.execute("DELETE FROM ts.observation")


@pytest.mark.asyncio(loop_scope="session")
async def test_lock_streams_outbox_denied_with_guc_off(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    """``LOCK TABLE … IN ROW EXCLUSIVE MODE`` requires INSERT/UPDATE/DELETE
    privilege on the target. The dashboard role has none, so this is
    privilege-blocked and survives the GUC override (unlike LOCK TABLE
    IN ACCESS SHARE MODE, which only needs SELECT — that one is GUC-only
    and is exercised in test_dashboard_soft_boundary_blocked_only_by_guc.py)."""
    await aslan_dashboard_conn.execute("SET default_transaction_read_only = off")
    await aslan_dashboard_conn.execute("BEGIN")
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await aslan_dashboard_conn.execute("LOCK TABLE streams.outbox IN ROW EXCLUSIVE MODE")
    finally:
        await aslan_dashboard_conn.execute("ROLLBACK")


@pytest.mark.asyncio(loop_scope="session")
async def test_redaction_registry_insert_function_denied_with_guc_off(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    """Migration 0020 explicitly REVOKEs EXECUTE on the v0.5 SECURITY
    DEFINER from ``aslan_dashboard``. EXECUTE is a privilege check — the
    GUC has nothing to do with it. Calling the function must fail even
    with the GUC off."""
    await aslan_dashboard_conn.execute("SET default_transaction_read_only = off")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await aslan_dashboard_conn.execute(
            "SELECT streams.redaction_registry_insert("
            "  gen_random_uuid(), 'Art.17', now(), 'kap', "
            "  '{}'::jsonb, repeat('a', 64), repeat('b', 64))"
        )
