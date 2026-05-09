"""POST to every registered route returns 405, except the small
allowlist of mutation routes carrying explicit audit-write workflows.

Spec §3 + §8.2: the v0.6.0 dashboard is read-only by default. The
unit test ``test_dashboard_routes_are_get_only.py`` enforces the
route-table layer; this integration test goes through the full ASGI
stack so a regression that registered a non-routing POST shim (e.g.
a middleware that dispatches POSTs) would still be caught.

The dq M2 spot-check labelling form (``/dq/spot-check/{sample_id}``)
is the lone mutation route. See
``aslan_core.dashboard.pages.dq_spot_check`` for the role + privilege
boundary contract — the dashboard role itself remains SELECT-only on
the underlying audit tables.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.routing import Route

from aslan_core.dashboard.app import app, configure_app

pytestmark = pytest.mark.integration

# Routes permitted to expose POST. Mirrors the unit-test allowlist
# in tests/unit/test_dashboard_routes_are_get_only.py — keep the two
# in sync. Entries here are excluded from the 405-on-POST sweep
# because their POST handler is the documented audit-write workflow.
_MUTATION_ROUTES: frozenset[str] = frozenset(
    {
        # dq M2: spot-check labelling form.
        "/dq/spot-check/{sample_id}",
        # dq M4: Bloomberg-comparison manual-entry form.
        "/dq/bloomberg/cells/{cell_id}",
        # dq M5: regression-flag review form.
        "/dq/validation/regression/{flag_id}",
        # NG6: external-corroborator refresh button.
        "/dq/spot-check/{sample_id}/corroborator/{source}/refresh",
    }
)


def _gettable_paths() -> list[str]:
    """Walk the route table and pick a representative request path
    for each non-mutation route. Mutation-allowlisted routes are
    excluded (they validate via the page-specific tests instead).

    For routes with path parameters we substitute a representative
    literal that the route's converter will accept — the PUT/POST
    405 check doesn't depend on the path-param value being meaningful.
    """
    paths: list[str] = []
    for route in app.routes:
        if not isinstance(route, Route) or not route.path:
            continue
        if route.path in _MUTATION_ROUTES:
            continue
        path = route.path
        # Substitute path-param placeholders with a benign literal so
        # the route resolves rather than 404-ing before the 405 check.
        if "{" in path:
            path = path.replace("{sample_id}", "00000000-0000-0000-0000-000000000000")
            path = path.replace("{cell_id}", "00000000-0000-0000-0000-000000000000")
            path = path.replace("{run_id}", "00000000-0000-0000-0000-000000000000")
            path = path.replace("{flag_id}", "00000000-0000-0000-0000-000000000000")
            # NG6 corroborator refresh route is parameterised by `{source}`
            # (kap_ir / investing_com); pick a representative literal so the
            # converter resolves rather than 404-ing before the 405 check.
            path = path.replace("{source}", "kap_ir")
        paths.append(path)
    return paths


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


@pytest.mark.parametrize("path", _gettable_paths())
@pytest.mark.asyncio(loop_scope="session")
async def test_post_to_registered_route_returns_405(
    _configured_dashboard: None,
    path: str,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(path)
    assert response.status_code == 405, (
        f"POST {path!r} returned {response.status_code}; expected 405 — "
        "the v0.6.0 dashboard is read-only at the route-table layer "
        "(except the named spot-check mutation route)"
    )
