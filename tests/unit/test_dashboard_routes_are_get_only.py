"""Every dashboard route is registered as GET-only (or HEAD/OPTIONS).

Spec §3 + §6.3: the v0.6.0 dashboard is read-only — no mutation
of any kind. The structural enforcement is at the route-table
layer: a route registered with ``@app.post`` would land before
the role-based isolation can refuse the SQL. This test inspects
the FastHTML / Starlette route table and asserts every route's
methods set is a subset of ``{GET, HEAD, OPTIONS}``.
"""

from __future__ import annotations

from starlette.routing import Route

from aslan_core.dashboard.app import app

_ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


def test_every_route_is_get_only() -> None:
    found_routes = 0
    for route in app.routes:
        if not isinstance(route, Route):
            # Mounts / WebSocket routes / etc. — none expected on the
            # dashboard, but the test stays narrow rather than asserting
            # the top-level shape.
            continue
        found_routes += 1
        methods = set(route.methods or ())
        unsafe = methods - _ALLOWED_METHODS
        assert not unsafe, (
            f"route {route.path!r} exposes unsafe methods {unsafe!r}; "
            f"the v0.6.0 dashboard is read-only"
        )
    assert found_routes > 0, "no routes registered on the dashboard app"
