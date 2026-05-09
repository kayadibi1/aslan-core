"""Smoke test that all seven /dq/* routes return 200 with stub content.

Stubs exist precisely so M1+ implementation can swap each page's
body without changing wiring. This test guards the wiring contract.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

# These imports register the routes via decorator side-effect at import time.
import aslan_core.dashboard.pages.dq_bloomberg as _dq_bloomberg  # noqa: F401
import aslan_core.dashboard.pages.dq_coverage as _dq_coverage  # noqa: F401
import aslan_core.dashboard.pages.dq_overview as _dq_overview  # noqa: F401
import aslan_core.dashboard.pages.dq_recency as _dq_recency  # noqa: F401
import aslan_core.dashboard.pages.dq_scorecard as _dq_scorecard  # noqa: F401
import aslan_core.dashboard.pages.dq_spot_check as _dq_spot_check  # noqa: F401
import aslan_core.dashboard.pages.dq_validation as _dq_validation  # noqa: F401
from aslan_core.dashboard.app import app

# This module's tests are sync (TestClient is sync; M0 stub pages don't
# touch the DB so no async session needed). Therefore: no
# pytest.mark.asyncio marker — that would conflict with strict-mode
# pytest-asyncio. The integration marker is kept so the test runs in
# the integration suite alongside the dq roundtrip tests.
pytestmark = pytest.mark.integration


ROUTES = [
    "/dq/overview",
    "/dq/recency",
    "/dq/coverage",
    "/dq/validation",
    "/dq/spot-check",
    "/dq/bloomberg",
    "/dq/scorecard",
]


@pytest.mark.parametrize("path", ROUTES)
def test_dq_route_returns_200(path: str) -> None:
    client = TestClient(app)
    resp = client.get(path)
    assert resp.status_code == 200
    assert "dq-stub" in resp.text
