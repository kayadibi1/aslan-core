"""NG1 — integration tests for the public ``/status`` route.

Verifies:

  1. The route returns 200 with no Authorization header (public).
  2. The route still returns 200 even WITH a (probably-bogus)
     Authorization header — no header check exists, so the page is
     deliberately not authenticated.
  3. The Cache-Control header is set to ``public, max-age=60``.
  4. The page renders the expected structure: headline + per-source
     rows + footer.
  5. The empty-data path renders 200 (no recency_observation rows
     -> all five sources surface as Unknown).
  6. The data-present path surfaces the right per-source state
     (lag within SLA -> ``Operational`` badge).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.public_status.app import app, configure_app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _configured_public_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Bind the app to the testcontainer session factory.

    The test fixture deliberately uses the testcontainer SUPERUSER
    role rather than ``public_status_reader`` because the empty-data
    pre-amble (cleaning audit.recency_observation across tests) needs
    DELETE privilege the public role lacks. The privilege boundary
    test lives in ``test_migration_0063_public_status_role.py``.
    """
    configure_app(session_factory=session_factory)
    yield


@pytest_asyncio.fixture(loop_scope="session")
async def _wipe_audit_observations(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Reset the two source tables before AND after each test so the
    empty-data and data-present cases don't bleed.
    """
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.recency_observation"))
        await s.execute(text("DELETE FROM audit.coverage_snapshot"))
        await s.commit()
    yield
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.recency_observation"))
        await s.execute(text("DELETE FROM audit.coverage_snapshot"))
        await s.commit()


# ── 1. No-auth ─────────────────────────────────────────────────────


async def test_status_route_returns_200_without_auth(
    _configured_public_status: None,
    _wipe_audit_observations: None,
) -> None:
    """GET /status with NO Authorization header returns 200."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200


async def test_status_route_does_not_check_authorization_header(
    _configured_public_status: None,
    _wipe_audit_observations: None,
) -> None:
    """The route does NOT inspect the Authorization header — a bogus
    one is irrelevant. Asserts the route is unguarded by design.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/status",
            headers={"Authorization": "Bearer totally-bogus-token"},
        )
    assert resp.status_code == 200


# ── 2. No auth import in the public_status package ────────────────


@pytest.mark.asyncio(loop_scope="session")
async def test_public_status_package_does_not_import_auth_helpers() -> None:
    """Source-level guard: no module under aslan_core.public_status
    imports any auth helper from aslan_core.api or similar.

    This is a deliberate canary — if a future refactor accidentally
    pulls a require_auth decorator from the API package into the
    public-status route, this test forces the developer to delete the
    import deliberately. We check imports, not free-text occurrences,
    so docstrings discussing the "no Authorization header check"
    posture do not trigger false positives.
    """
    import ast
    import inspect

    from aslan_core.public_status import app as app_mod
    from aslan_core.public_status import queries as q_mod
    from aslan_core.public_status import render as r_mod
    from aslan_core.public_status import view_models as vm_mod
    from aslan_core.public_status.pages import status as status_mod

    forbidden_modules = {"aslan_core.api.auth", "aslan_core.api.deps"}
    forbidden_names = {"require_auth", "verify_token", "get_current_user"}
    for module in (app_mod, q_mod, r_mod, vm_mod, status_mod):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in forbidden_modules, (
                    f"{module.__name__} imports {node.module!r}"
                )
                for alias in node.names:
                    assert alias.name not in forbidden_names, (
                        f"{module.__name__} imports {alias.name!r}"
                    )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden_modules, (
                        f"{module.__name__} imports {alias.name!r}"
                    )


# ── 3. Cache-Control ──────────────────────────────────────────────


async def test_status_route_sets_cache_control(
    _configured_public_status: None,
    _wipe_audit_observations: None,
) -> None:
    """Cache-Control: public, max-age=60 — supports CDN absorption."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    cache_control = resp.headers.get("Cache-Control") or resp.headers.get("cache-control")
    assert cache_control == "public, max-age=60"


# ── 4. Page structure ─────────────────────────────────────────────


async def test_status_route_renders_expected_structure(
    _configured_public_status: None,
    _wipe_audit_observations: None,
) -> None:
    """The page renders headline + footer + an entry for each source."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.text
    # Headline is always rendered (even in empty-data case).
    assert "% Fresh" in body
    # Each public source must appear in the body.
    for src in ("KAP", "EVDS", "BIST", "TEFAS", "MKK"):
        assert src in body
    # Footer link to terms is rendered.
    assert "Powered by" in body
    assert "Aslan Terminal" in body


# ── 5. Empty-data path ────────────────────────────────────────────


async def test_status_route_empty_data_renders_unknown_badges(
    _configured_public_status: None,
    _wipe_audit_observations: None,
) -> None:
    """No recency_observation rows -> every source surfaces UNKNOWN."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.text
    # The data-attr on the per-source card carries the badge enum
    # value verbatim — easier to assert against than the visible label.
    assert body.count('data-badge="unknown"') == 5
    assert "0% Fresh" in body  # No observations -> 0 overall


# ── 6. Data-present path ──────────────────────────────────────────


async def test_status_route_with_data_renders_ok_badge(
    _configured_public_status: None,
    _wipe_audit_observations: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Insert a single fresh KAP observation -> KAP renders OK."""
    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO audit.recency_observation"
                "(source, observed_at, upstream_latest_at, "
                " db_latest_at, sla_target_seconds) "
                "VALUES ('kap', now(), now(), now(), 300)"
            )
        )
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.text
    # KAP card renders OK; the four other sources stay UNKNOWN.
    assert 'data-source="kap"' in body
    assert 'data-badge="ok"' in body
    assert body.count('data-badge="unknown"') == 4
    # Headline averages over observed sources only -> 100% in this case.
    assert "100% Fresh" in body
