"""SECURITY DEFINER audit helpers run as a no-login dedicated role.

Codex post-implementation review (HIGH): migrations 0021 + 0022
gave the broad runtime ``aslan_app`` role direct ``SELECT`` on
``audit.events.metadata`` and ``audit.events.client_ip`` so the
two SECURITY DEFINER helpers could read them. That expanded
``aslan_app``'s privileges — any compromised path or SQL injection
running as the application role would also have raw audit PII
access, defeating the dashboard's "derived value" design.

Migration 0024 introduces ``aslan_audit_helpers`` (NOLOGIN),
transfers ownership of both helpers to it, and REVOKEs the column-
level grants from ``aslan_app``. After the migration:

  * ``aslan_audit_helpers`` is the function owner; the SECURITY
    DEFINER body runs with that role's column SELECTs.
  * ``aslan_app`` can NO LONGER directly SELECT
    ``audit.events.metadata`` or ``audit.events.client_ip``.
  * The dashboard role still has ``EXECUTE`` on both helpers, so
    derived values continue to flow through.

This file pins the contract.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_helpers_owner_role_exists(session: AsyncSession) -> None:
    rolname = (
        await session.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname = 'aslan_audit_helpers'")
        )
    ).scalar_one_or_none()
    assert rolname == "aslan_audit_helpers"


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_helpers_owner_is_nologin(session: AsyncSession) -> None:
    """The owner role MUST NOT be able to log in. A login-capable
    helper-owner role would expand the surface to whoever can read
    the password file (or env var)."""
    rolcanlogin = (
        await session.execute(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'aslan_audit_helpers'")
        )
    ).scalar_one()
    assert rolcanlogin is False


@pytest.mark.parametrize(
    "fn_signature",
    [
        "audit.event_metadata_key_count(BIGINT, TIMESTAMPTZ)",
        "audit.event_client_ip_truncated(BIGINT, TIMESTAMPTZ)",
    ],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_helper_owner_is_aslan_audit_helpers(
    session: AsyncSession,
    fn_signature: str,
) -> None:
    """``pg_proc.proowner`` resolves to ``aslan_audit_helpers``. A
    regression that re-set the owner to ``aslan_app`` (or any
    login-capable role) trips here."""
    owner_oid = (
        await session.execute(
            text(f"SELECT proowner FROM pg_proc WHERE oid = '{fn_signature}'::regprocedure")  # noqa: S608
        )
    ).scalar_one()
    expected_oid = (
        await session.execute(
            text("SELECT oid FROM pg_roles WHERE rolname = 'aslan_audit_helpers'")
        )
    ).scalar_one()
    assert owner_oid == expected_oid


@pytest.mark.parametrize("column", ["metadata", "client_ip"])
@pytest.mark.asyncio(loop_scope="session")
async def test_aslan_app_cannot_select_forbidden_audit_column(
    session: AsyncSession,
    column: str,
) -> None:
    """``has_column_privilege`` is the canonical privilege check —
    returns false for ``aslan_app`` on the column. The function
    bodies above run as ``aslan_audit_helpers``, which DOES hold the
    grant; ``aslan_app`` does not."""
    has_priv = (
        await session.execute(
            text("SELECT has_column_privilege('aslan_app', 'audit.events', :col, 'SELECT')"),
            {"col": column},
        )
    ).scalar_one()
    assert has_priv is False, (
        f"aslan_app retains SELECT on audit.events.{column} — migration 0024 "
        "expected to revoke it. The runtime application role must not have "
        "raw audit PII access; only the dashboard role can reach derived "
        "values via the SECURITY DEFINER helpers."
    )


@pytest.mark.parametrize("column", ["event_id", "occurred_at", "metadata", "client_ip"])
@pytest.mark.asyncio(loop_scope="session")
async def test_audit_helpers_role_has_required_column_grants(
    session: AsyncSession,
    column: str,
) -> None:
    """The helper bodies need SELECT on the underlying columns — if
    a future migration revokes one of them by accident, the
    SECURITY DEFINER body will fail at runtime when called from a
    page handler. Pin the four-column GRANT here."""
    has_priv = (
        await session.execute(
            text(
                "SELECT has_column_privilege('aslan_audit_helpers', 'audit.events', :col, 'SELECT')"
            ),
            {"col": column},
        )
    ).scalar_one()
    assert has_priv is True, (
        f"aslan_audit_helpers lacks SELECT on audit.events.{column}. "
        "The SECURITY DEFINER helper bodies require this grant; without "
        "it the dashboard /audit page renders with errors."
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_dashboard_role_can_still_execute_helpers(session: AsyncSession) -> None:
    """The whole point of the dedicated owner role is that the
    dashboard's EXECUTE grant continues to work after the owner
    swap. Verify the EXECUTE ACL survived migration 0024."""
    for fn in (
        "event_metadata_key_count(BIGINT, TIMESTAMPTZ)",
        "event_client_ip_truncated(BIGINT, TIMESTAMPTZ)",
    ):
        has_priv = (
            await session.execute(
                text("SELECT has_function_privilege('aslan_dashboard', :fn, 'EXECUTE')"),
                {"fn": f"audit.{fn}"},
            )
        ).scalar_one()
        assert has_priv is True, (
            f"aslan_dashboard lost EXECUTE on audit.{fn}; the /audit page "
            "now renders with errors. Migration 0024's ALTER FUNCTION "
            "OWNER should have preserved the EXECUTE GRANT."
        )
