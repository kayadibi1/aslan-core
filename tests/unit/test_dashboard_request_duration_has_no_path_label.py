"""``aslan_dashboard_request_duration_seconds`` has no labels.

Spec §6.3 round-4 + Task 14: per-route timing is a side-channel.
The histogram observes a single global distribution so a metric
observer cannot correlate a fast response with the no-rows path
on a deadletter render or a slow response with a backend hot key.
P99 latency alerting is preserved (one global metric); the
per-surface breakdown deliberately is not.

This test pins the contract: the histogram's labelnames tuple is
empty. A future PR that adds a ``route`` or ``status`` label to
the duration histogram trips here before any time-series lands.
"""

from __future__ import annotations

from aslan_core.observability.metrics import (
    dashboard_request_duration_seconds,
    dashboard_requests_total,
)


def test_request_duration_histogram_has_no_labels() -> None:
    assert dashboard_request_duration_seconds.labelnames == ()


def test_request_counter_has_path_and_status_labels_only() -> None:
    """Sibling assertion — the counter does carry labels (path,
    status). A regression that added a third label would expand
    cardinality and likely defeat the closed-enum guarantees in
    test_dashboard_metrics_labels_are_closed_enum.py."""
    assert dashboard_requests_total.labelnames == ("path", "status")
