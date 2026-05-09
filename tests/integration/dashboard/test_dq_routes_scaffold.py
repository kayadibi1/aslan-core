"""Smoke test that all seven /dq/* routes return 200.

The four still-stubbed routes (/dq/validation, /dq/spot-check,
/dq/bloomberg, /dq/scorecard) render the shared M0 stub body
without touching the DB. The three M1 routes (/dq/overview,
/dq/recency, /dq/coverage) hit `audit.*` tables; the test drives
them through `configure_app(session_factory=..., redis_client=...)`
so they get a session at runtime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.testclient import TestClient

# These imports register the routes via decorator side-effect at import time.
import aslan_core.dashboard.pages.dq_bloomberg as _dq_bloomberg  # noqa: F401
import aslan_core.dashboard.pages.dq_coverage as _dq_coverage  # noqa: F401
import aslan_core.dashboard.pages.dq_overview as _dq_overview  # noqa: F401
import aslan_core.dashboard.pages.dq_recency as _dq_recency  # noqa: F401
import aslan_core.dashboard.pages.dq_scorecard as _dq_scorecard  # noqa: F401
import aslan_core.dashboard.pages.dq_spot_check as _dq_spot_check  # noqa: F401
import aslan_core.dashboard.pages.dq_validation as _dq_validation  # noqa: F401
from aslan_core.dashboard.app import app, configure_app

pytestmark = pytest.mark.integration


# All seven /dq/* routes hit audit.* tables on this branch.
# /dq/spot-check moved off the stub list in M2 — see
# tests/integration/dashboard/test_dq_spot_check.py for the
# pending-queue + sample-form coverage.
# /dq/bloomberg moved off the stub list in M4 — see
# tests/integration/dashboard/test_dq_bloomberg.py for the per-cell
# entry form + history coverage.
# /dq/validation moved off the stub list in M5 — see
# tests/integration/dashboard/test_dq_validation.py for the rule /
# regression-flag / xs-skip surfaces and the regression review POST.
# /dq/scorecard moved off the stub list in M6 — backed by
# audit.scorecard_snapshot via dashboard.queries.dq_scorecard.
STUB_ROUTES: list[str] = []

M1_M2_ROUTES = [
    "/dq/overview",
    "/dq/recency",
    "/dq/coverage",
    "/dq/spot-check",
    "/dq/bloomberg",
    "/dq/validation",
    "/dq/scorecard",
]


@pytest.mark.parametrize("path", STUB_ROUTES)
def test_dq_stub_route_returns_200(path: str) -> None:
    """The remaining stub routes do not need configure_app — they render
    a static DqStubVM body without touching the DB.

    Currently empty (M6 moved /dq/scorecard off the stub list). The
    parametrize keeps this test in the suite as a placeholder; future
    new /dq/* routes can land here briefly during their stub phase.
    """
    client = TestClient(app)
    resp = client.get(path)
    assert resp.status_code == 200
    assert "dq-stub" in resp.text


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


@pytest.mark.parametrize("path", M1_M2_ROUTES)
@pytest.mark.asyncio(loop_scope="session")
async def test_dq_m1_m2_route_renders(_configured_dashboard: None, path: str) -> None:
    """The four M1+M2 routes render against the configured testcontainer
    DB. Empty-data path: no recency_observation / coverage_snapshot /
    spot_check_sample rows are required for the routes to return 200."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(path)
    assert resp.status_code == 200
    assert "Data Quality" in resp.text
