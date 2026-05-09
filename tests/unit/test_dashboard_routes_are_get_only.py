"""Every dashboard route is registered as GET-only (or HEAD/OPTIONS),
except the small allowlist of mutation routes carrying explicit
audit-write workflows.

Spec §3 + §6.3: the v0.6.0 dashboard is read-only by default — no
mutation of any kind through the dashboard role. The structural
enforcement is at the route-table layer: a route registered with
``@app.post`` would land before the role-based isolation can refuse
the SQL.

Spec §5.5 + §7.2 + dq M2: the spot-check labelling workflow REQUIRES
a POST endpoint (``/dq/spot-check/{sample_id}``). The labeller
authenticates against a write-capable session (``audit_writer`` role
holds the narrow column-level UPDATE on ``audit.spot_check_sample``
plus INSERT on ``audit.spot_check_result``); the dashboard role
itself remains read-only. This test asserts every dashboard route
falls into one of two buckets: GET-only, or one of the named
mutation routes on the labelling allowlist.
"""

from __future__ import annotations

from starlette.routing import Route

from aslan_core.dashboard.app import app

_ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

# Routes permitted to register a POST handler. Each entry MUST have
# an associated audit-write workflow + a separate role-level
# justification documented in the page module's docstring.
_MUTATION_ROUTES: frozenset[str] = frozenset(
    {
        # dq M2: spot-check labelling form. See
        # aslan_core.dashboard.pages.dq_spot_check for the role +
        # privilege boundary contract.
        "/dq/spot-check/{sample_id}",
    }
)
# POST is the only mutation method permitted on allowlisted routes.
_ALLOWED_MUTATION_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "POST"})


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
        if route.path in _MUTATION_ROUTES:
            unsafe = methods - _ALLOWED_MUTATION_METHODS
            assert not unsafe, (
                f"mutation-allowlisted route {route.path!r} exposes "
                f"unexpected methods {unsafe!r}"
            )
        else:
            unsafe = methods - _ALLOWED_METHODS
            assert not unsafe, (
                f"route {route.path!r} exposes unsafe methods {unsafe!r}; "
                f"the v0.6.0 dashboard is read-only"
            )
    assert found_routes > 0, "no routes registered on the dashboard app"
