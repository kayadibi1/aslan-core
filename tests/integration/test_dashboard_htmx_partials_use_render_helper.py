"""htmx partial endpoints use the render() helper.

Spec §6.3 + §8.2: the central ``render(request, vm)`` helper is
the single output point. Every htmx partial endpoint MUST call
through it so the runtime VM-type check, ``model_dump(mode='json')``
materialisation, and template lookup fire on every response.

v0.6.0 has no htmx partial endpoints yet — the surface is full
pages only (see app.py's route table). This test exists so that
when the first partial lands (v0.7.x ``/api/cards/outbox`` shape),
the render-helper-only contract is asserted from day one.

The implementation walks the route table for paths starting with
``/api/``, asserts each goes through the helper. The current
empty-list state is checked explicitly so a future add of a
partial route triggers an assertion failure naming the new path
— the engineer reads the message and adds the corresponding
seed + sentinel coverage in the matrix.
"""

from __future__ import annotations

from starlette.routing import Route

from aslan_core.dashboard.app import app


def test_no_htmx_partials_yet_in_v0_6_0() -> None:
    """When the first ``/api/...`` route lands, this test fails;
    the developer adds the partial to the matrix in
    ``test_dashboard_sentinel_matrix.py`` and updates the
    ``return render(...)`` walk in
    ``test_dashboard_render_helper_only_path.py`` to cover it."""
    api_routes = [
        route.path
        for route in app.routes
        if isinstance(route, Route) and route.path.startswith("/api/")
    ]
    assert api_routes == [], (
        f"htmx partial routes registered: {api_routes!r}. v0.6.0 ships "
        "no partials; when the first one lands, extend the sentinel "
        "matrix and the render-helper walk to cover the new endpoint, "
        "then update this test to enumerate the partials and assert "
        "render-helper usage explicitly."
    )
