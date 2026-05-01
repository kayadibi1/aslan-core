"""Migration 0020 — dashboard role + GRANTs + audit metadata helper.

Sanity checks against the testcontainer Postgres after `alembic upgrade
head` runs the migration. Deeper boundary tests live in the dashboard
v0.6.0 boundary test suite.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_aslan_dashboard_role_exists(session: AsyncSession) -> None:
    role = (
        await session.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'aslan_dashboard'"))
    ).scalar_one_or_none()
    assert role is not None


async def test_aslan_dashboard_can_login(session: AsyncSession) -> None:
    rolcanlogin = (
        await session.execute(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'aslan_dashboard'")
        )
    ).scalar_one()
    assert rolcanlogin is True


async def test_aslan_dashboard_default_transaction_read_only(
    session: AsyncSession,
) -> None:
    setting = (
        await session.execute(
            text(
                "SELECT setconfig FROM pg_db_role_setting "
                "JOIN pg_roles ON pg_db_role_setting.setrole = pg_roles.oid "
                "WHERE rolname = 'aslan_dashboard'"
            )
        )
    ).scalar_one_or_none()
    assert setting is not None
    assert any("default_transaction_read_only=on" in s for s in setting)


async def test_event_metadata_key_count_helper_owned_by_aslan_app(
    session: AsyncSession,
) -> None:
    proowner = (
        await session.execute(
            text(
                "SELECT r.rolname FROM pg_proc p "
                "JOIN pg_namespace n ON p.pronamespace = n.oid "
                "JOIN pg_roles r ON p.proowner = r.oid "
                "WHERE n.nspname = 'audit' AND p.proname = 'event_metadata_key_count'"
            )
        )
    ).scalar_one_or_none()
    assert proowner == "aslan_app"


async def test_event_metadata_key_count_is_security_definer(
    session: AsyncSession,
) -> None:
    prosecdef = (
        await session.execute(
            text(
                "SELECT prosecdef FROM pg_proc p "
                "JOIN pg_namespace n ON p.pronamespace = n.oid "
                "WHERE n.nspname = 'audit' AND p.proname = 'event_metadata_key_count'"
            )
        )
    ).scalar_one()
    assert prosecdef is True


async def test_aslan_dashboard_password_guc_released_post_migration(
    session: AsyncSession,
) -> None:
    """`set_config(..., true)` releases at COMMIT — the GUC must NOT
    be visible outside the migration's transaction (codex plan-round-2)."""
    value = (
        await session.execute(text("SELECT current_setting('aslan.dashboard_password', true)"))
    ).scalar_one()
    assert value is None or value == ""
