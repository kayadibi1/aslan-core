"""``/metrics`` returns a non-empty Prometheus exposition body.

Codex post-implementation review (medium): the
``aslan-core[dashboard]`` extra MUST include ``prometheus-client``
so a documented dashboard-only install ships working metrics. The
empty-body fallback in ``metrics_endpoint`` exists as a defensive
measure, NOT as a deployable shape — operators relying on the 500-
spike signal would silently lose it if ``prometheus-client`` were
missing from the extra.

This test ensures any future PR that drops ``prometheus-client``
from the dashboard runtime path trips before reaching CI's
integration tier.
"""

from __future__ import annotations

import asyncio


def test_metrics_endpoint_returns_non_empty_body() -> None:
    """If ``prometheus-client`` is missing from the dashboard
    runtime path, ``metrics_endpoint`` returns ``b""`` and this
    assertion fails. The same path serves the live ``/metrics``
    route; the unit-level check catches the regression without
    spinning up the testcontainer stack."""
    from aslan_core.dashboard.metrics import metrics_endpoint

    body, content_type = asyncio.run(metrics_endpoint())
    assert body != b"", (
        "metrics_endpoint() returned an empty body — prometheus-client "
        "is missing from the runtime install. The [dashboard] extra MUST "
        "include prometheus-client so /metrics ships working signals."
    )
    # Prometheus exposition format starts with HELP / TYPE comments OR
    # a metric family; any way the bytes are non-empty plus the
    # documented content type is a valid response.
    assert content_type.startswith(("text/plain", "application/openmetrics-text"))


def test_dashboard_extra_pins_prometheus_client() -> None:
    """Pin the contract at the manifest layer too — a future
    refactor that moves the dependency into a different extra
    breaks the dashboard-only install we ship to operators."""
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads(
        (Path(__file__).parent.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    dashboard_deps = pyproject["project"]["optional-dependencies"]["dashboard"]
    has_prom = any(dep.startswith("prometheus-client") for dep in dashboard_deps)
    assert has_prom, (
        "[project.optional-dependencies] dashboard does not include "
        "prometheus-client. The dashboard's /metrics endpoint relies on "
        "it; without it operators see an empty-body response and lose "
        "the 500-spike alert signal."
    )
