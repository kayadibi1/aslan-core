"""Per-request session has ``transaction_read_only=on``.

Spec §5.1 + §8.2: migration 0020 sets
``ALTER ROLE aslan_dashboard SET default_transaction_read_only = on``
so every connection the role opens lands in a read-only
transaction.

The hard-boundary tests (which assert specific operations raise)
live in ``test_dashboard_soft_boundary_blocked_only_by_guc.py``
where they exercise raw asyncpg with the right error type. This
file pins the contract that the SQLAlchemy session layer the
dashboard's request handlers actually use carries the GUC — a
regression that opened a session through a different DSN (e.g.
the writer role) would lose ``read_only=on`` and not raise here.

The pg_advisory_lock + SELECT FOR UPDATE specific assertions
listed in the spec are covered by the soft-boundary file using
the asyncpg-native error type. This file's job is to verify the
GUC value, not the operation outcome.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_default_transaction_read_only_is_on(
    aslan_dashboard_session: AsyncSession,
) -> None:
    value = (
        await aslan_dashboard_session.execute(
            text("SELECT current_setting('transaction_read_only')")
        )
    ).scalar_one()
    assert value == "on", (
        f"transaction_read_only={value!r}; expected 'on'. The dashboard "
        "role's role-level GUC (migration 0020) is the load-bearing "
        "soft floor — a regression that opened the session through a "
        "different DSN would lose this default."
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_session_user_is_aslan_dashboard(
    aslan_dashboard_session: AsyncSession,
) -> None:
    """Session-layer mirror of ``test_dashboard_session_role_is_aslan_dashboard.py``
    that hits the ASGI app — this one queries the SQLAlchemy session
    directly, no FastHTML in the loop. Both tests should pass; if
    one fails and the other doesn't, the divergence points to a
    middleware-layer regression (e.g. middleware swapped the
    session_factory mid-request)."""
    user = (await aslan_dashboard_session.execute(text("SELECT current_user"))).scalar_one()
    assert user == "aslan_dashboard"
