"""Closed-enum metric labels for the dashboard.

Spec §6.3 round-5 + Task 14: ``aslan_dashboard_requests_total``
labels (``path``, ``status``) are bounded sets — unbounded label
cardinality is the canonical Prometheus footgun, AND a per-route
label here would tell a metric observer which compliance-sensitive
surface an operator hit. The closed enum is the structural fix.

The tests here exercise the bucketing helpers directly so the
contract holds without requiring a live Prometheus registry — the
helpers are the single source of truth, and a regression that
broadens either enum trips a unit test before it ships.
"""

from __future__ import annotations

import pytest

from aslan_core.dashboard.metrics import bucket_path, bucket_status
from aslan_core.observability.metrics import (
    _KNOWN_DASHBOARD_PATHS,
    _KNOWN_DASHBOARD_STATUSES,
)

# ── Path enum ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/outbox",
        "/streams",
        "/ingestion",
        "/documents",
        "/timeseries",
        "/deadletter",
        "/metrics",
    ],
)
def test_canonical_path_passes_through(path: str) -> None:
    assert bucket_path(path) == path


@pytest.mark.parametrize("path", ["/audit", "/redactions"])
def test_compliance_sensitive_paths_collapse_to_sensitive_bucket(path: str) -> None:
    assert bucket_path(path) == "<sensitive>"


@pytest.mark.parametrize(
    "path",
    [
        "/static/dashboard.css",
        "/static/htmx.min.js",
        "/static/favicon.ico",
        "/static/anything-else",
    ],
)
def test_static_paths_collapse_under_static_label(path: str) -> None:
    assert bucket_path(path) == "/static"


@pytest.mark.parametrize(
    "path",
    [
        "/no-such-route",
        "/api/audit/partial",
        "/audit/extra",
        "/audit?q=1",
        "",
        "/foo/bar/baz",
    ],
)
def test_unknown_paths_collapse_to_other(path: str) -> None:
    assert bucket_path(path) == "<other>"


def test_path_label_is_always_in_closed_enum() -> None:
    """Every output of ``bucket_path`` is a member of the
    documented closed enum. A regression that introduced a new
    label string would land here before reaching Prometheus."""
    samples = [
        "/",
        "/outbox",
        "/streams",
        "/ingestion",
        "/documents",
        "/timeseries",
        "/deadletter",
        "/audit",
        "/redactions",
        "/metrics",
        "/static/dashboard.css",
        "/static/htmx.min.js",
        "/static/favicon.ico",
        "/no-such-route",
        "/api/audit/partial",
        "",
        "/foo/bar/baz",
    ]
    for path in samples:
        assert bucket_path(path) in _KNOWN_DASHBOARD_PATHS, (
            f"path bucket {bucket_path(path)!r} is not in the closed enum"
        )


# ── Status enum ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "expected"), [(200, "200"), (404, "404"), (405, "405"), (500, "500")]
)
def test_canonical_status_passes_through(code: int, expected: str) -> None:
    assert bucket_status(code) == expected


@pytest.mark.parametrize("code", [201, 204, 301, 302, 401, 403, 502, 503, 0, 999])
def test_other_statuses_collapse_to_other(code: int) -> None:
    assert bucket_status(code) == "<other>"


def test_status_label_is_always_in_closed_enum() -> None:
    """Every output of ``bucket_status`` is a member of the
    documented closed enum."""
    for code in (200, 201, 301, 404, 405, 418, 500, 503, 0, 999):
        assert bucket_status(code) in _KNOWN_DASHBOARD_STATUSES
