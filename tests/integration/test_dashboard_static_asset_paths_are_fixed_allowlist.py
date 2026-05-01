"""Static asset routes serve only the three-element allowlist.

Spec §6.3 finding 6 + §8.2: the dashboard exposes exactly three
static paths. Anything else under ``/static/`` returns 404. There
is no path-traversal route — the route table maps fixed paths to
fixed asset names, so ``/static/../something`` resolves to the
404 handler at the URL-routing layer (not at the asset lookup
layer). The 200 responses carry the documented Content-Type and
Cache-Control headers.

The tests here cover the HTTP surface; ``serve_static`` is unit-
tested separately to verify the ``importlib.resources`` lookup
path stays inside the assets package even when the function is
called with a fabricated ``name``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.dashboard.app import app, configure_app

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
) -> AsyncIterator[None]:
    configure_app(session_factory=session_factory, redis_client=redis_client)
    yield


# ── 200 responses: each allowlisted asset returns its bytes ──────


@pytest.mark.parametrize(
    ("path", "expected_ct_prefix", "expected_cache"),
    [
        ("/static/dashboard.css", "text/css", "public, max-age=86400"),
        (
            "/static/htmx.min.js",
            "application/javascript",
            "public, max-age=86400, immutable",
        ),
        ("/static/favicon.ico", "image/x-icon", "public, max-age=86400"),
    ],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_allowlisted_path_serves_with_documented_headers(
    _configured_dashboard: None,
    path: str,
    expected_ct_prefix: str,
    expected_cache: str,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(expected_ct_prefix)
    assert response.headers["cache-control"] == expected_cache
    assert len(response.content) > 0


# ── 404 responses: anything outside the allowlist ────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/static/htmx.min.js.map",
        "/static/dashboard.css.map",
        "/static/anything-not-in-the-three.txt",
        "/static/htmx.min.js.sha256",  # the sidecar is internal — not served
        "/static/../etc/passwd",  # URL-routing rejects, never reaches handler
        "/static/subdir/dashboard.css",
        "/static/",
        "/static",
    ],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_non_allowlisted_path_returns_404(
    _configured_dashboard: None,
    path: str,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(path)
    assert response.status_code == 404


# ── Negative shapes — ensure the static handler is GET-only ──────


@pytest.mark.parametrize(
    "path",
    ["/static/dashboard.css", "/static/htmx.min.js", "/static/favicon.ico"],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_static_routes_are_get_only(
    _configured_dashboard: None,
    path: str,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(path)
    assert response.status_code == 405


@pytest.mark.asyncio(loop_scope="session")
async def test_htmx_response_starts_with_module_iife_marker(
    _configured_dashboard: None,
) -> None:
    """htmx 1.9.x's minified file starts with the AMD/CommonJS/global
    UMD wrapper. A regression that swaps the body for an empty file or
    a stub would not match the prefix."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/static/htmx.min.js")
    assert response.text.startswith("(function(e,t)")


@pytest.mark.asyncio(loop_scope="session")
async def test_pages_link_static_with_sri_integrity(
    _configured_dashboard: None,
) -> None:
    """Every page rendered through the base template must carry the
    SRI integrity attribute on the htmx <script>. A future refactor
    that drops integrity= would silently lose tamper-evidence on the
    one third-party JS asset we ship."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/")
    body = response.text
    assert "/static/htmx.min.js" in body
    assert "integrity=" in body
    assert "sha256-" in body
    assert 'crossorigin="anonymous"' in body
