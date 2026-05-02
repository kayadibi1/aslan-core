"""Soft boundary: ``default_transaction_read_only=on`` blocks operations
that the privilege layer alone would let through.

Spec §5.1 distinguishes hard boundaries (privilege-enforced; covered by
``test_dashboard_hard_boundary_survives_guc_override.py``) from soft
boundaries (GUC-enforced; this file). A soft boundary is an operation
the role HAS the privilege for but the read-only GUC still rejects.

The two phases:

Phase 1 (GUC on — the role default): the operation raises
``ReadOnlySqlTransactionError`` (SQLSTATE 25006). This is the user-
visible behavior production relies on.

Phase 2 (GUC explicitly off): the SAME operation succeeds. Encoded via
``pytest.mark.xfail(strict=True)`` — the test body asserts the operation
raises, which it WILL NOT in current PG, so the test xfails. If a future
PG version starts blocking these at the privilege layer too (e.g. the
read-only check moves earlier or new privilege rules apply), the
operation will raise, the test will xpass, and ``strict=True`` flags it
as a failure — forcing us to update spec §5.1's claim that these are
GUC-only.

Notes on operation selection (deviations from spec §8.2):

  * ``LOCK TABLE … IN ACCESS SHARE MODE`` was originally listed as a
    soft-boundary case but PG's read-only check only blocks LOCK modes
    strictly higher than ROW EXCLUSIVE (see ``src/backend/tcop/utility.c``
    ``CheckRestrictedOperation``). ACCESS SHARE is below that threshold
    and is allowed in read-only transactions. Higher LOCK modes are
    also privilege-blocked (the dashboard role has no
    UPDATE/DELETE/TRUNCATE), so ``LOCK TABLE`` collapses into the hard-
    boundary file.

  * ``pg_advisory_lock`` was claimed by codex round-6 to raise
    ``ReadOnlySqlTransaction`` under the role default. Empirical run
    against TimescaleDB ``timescale/timescaledb:latest-pg16`` returned
    success — PG does NOT classify advisory-lock acquisition as a
    write for read-only-transaction enforcement. The claim was
    speculative; real behavior is that advisory locks are allowed
    under ``default_transaction_read_only=on``. Dropped from this
    file's coverage. (The defensive posture still holds: advisory
    locks against application keys are an aslan-app-side concern and
    the dashboard role would still hit privilege-revoked
    ``streams.redaction_registry_insert`` before reaching any
    dashboard-relevant advisory lock — covered in the hard-boundary
    file.)

The remaining soft-boundary case is ``SELECT … FOR UPDATE``: the
privilege layer permits it (SELECT GRANT covers ROW SHARE table lock),
the read-only GUC blocks it.
"""

from __future__ import annotations

import asyncpg
import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_select_for_update_raises_read_only_with_default_guc(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    """Phase 1: with the role default ``default_transaction_read_only=on``,
    ``SELECT … FOR UPDATE`` raises 25006."""
    with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
        await aslan_dashboard_conn.execute("SELECT 1 FROM streams.outbox FOR UPDATE")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "soft-blocked: with default_transaction_read_only=off, SELECT FOR UPDATE "
        "succeeds. If a future PG version blocks this at the privilege layer "
        "too, this test XPASSes and strict=True surfaces the change — update "
        "spec §5.1 to reflect the new reality."
    ),
)
@pytest.mark.asyncio(loop_scope="session")
async def test_select_for_update_succeeds_with_guc_off(
    aslan_dashboard_conn: asyncpg.Connection,
) -> None:
    """Phase 2: same operation under ``default_transaction_read_only=off``
    must NOT raise. Encoded as xfail-strict so that if a future PG
    version starts blocking this (e.g. moves the read-only check to the
    privilege layer), the test XPASSes and forces a spec update."""
    await aslan_dashboard_conn.execute("SET default_transaction_read_only = off")
    with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
        await aslan_dashboard_conn.execute("SELECT 1 FROM streams.outbox FOR UPDATE")
