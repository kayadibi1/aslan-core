"""The dashboard's per-request DB session uses ``aslan_dashboard``.

Spec §8.2: the production deployment binds the FastHTML app to a
session_factory built from ``ASLAN_DASHBOARD_DSN`` (the dedicated
LOGIN role). Tests usually wire the testcontainer superuser for
convenience, but the role boundary is the load-bearing GDPR +
read-only floor — a regression that swapped the role would not
fail any of the projection-layer tests, so we verify it
explicitly here by reconfiguring the app with an
``aslan_dashboard`` engine and inspecting ``current_user`` from
inside a request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fasthtml.common import P
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard.app import app, configure_app, get_session_factory
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import _VMBase

pytestmark = pytest.mark.integration


class _RoleProbeVM(_VMBase):
    """Single-purpose VM used by the probe route below."""

    role: str


def _probe_body(vm: _RoleProbeVM) -> object:
    return P(f"current_user={vm.role}", id="role-probe")


register_template(_RoleProbeVM, _probe_body)


@app.get("/_test/_role")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def _role_probe(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        role = (await session.execute(text("SELECT current_user"))).scalar_one()
    return render(request, _RoleProbeVM(role=str(role)))


@pytest_asyncio.fixture(loop_scope="session")
async def _aslan_dashboard_session_factory(
    aslan_dashboard_dsn: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = create_async_engine(
        aslan_dashboard_dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio(loop_scope="session")
async def test_request_session_is_aslan_dashboard(
    _aslan_dashboard_session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> None:
    configure_app(
        session_factory=_aslan_dashboard_session_factory,
        redis_client=redis_client,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/_test/_role")
    assert response.status_code == 200
    assert "current_user=aslan_dashboard" in response.text
