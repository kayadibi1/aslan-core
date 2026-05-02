"""Dashboard request metrics — Task 14.

Spec §6.3 round-4 + round-5: every HTTP request the dashboard
serves increments a request counter and observes a duration. The
counter is labelled by closed-enum (path, status); the histogram
is unlabelled.

Two side-channel guarantees:

  * **<sensitive> bucketing.** ``/audit`` and ``/redactions``
    collapse to the literal label ``"<sensitive>"`` so a metric
    observer cannot tell which compliance-sensitive surface an
    operator hit. Spec §6.2: until ``aslan-service`` ships
    per-request operator identity, the dashboard makes no
    per-view-attribution claim.

  * **Unlabelled duration histogram.** Per-route timing is a
    side-channel that would let an observer correlate a fast
    response with the no-rows path on a deadletter page (or a
    slow response with a backend hot key). The histogram observes
    a single global distribution; alerting on p99 latency stays
    actionable without leaking surface.

Path bucketing:

  * Exact matches for the canonical operator routes (``/``,
    ``/outbox``, ``/streams``, ``/ingestion``, ``/documents``,
    ``/timeseries``, ``/deadletter``, ``/metrics``).
  * ``/audit`` + ``/redactions`` → ``"<sensitive>"``.
  * Anything under ``/static/`` → ``"/static"``.
  * Anything else → ``"<other>"``.

Status bucketing:

  * ``200`` / ``404`` / ``405`` / ``500`` are kept verbatim.
  * Anything else collapses to ``"<other>"``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from aslan_core.observability.metrics import (
    _KNOWN_DASHBOARD_PATHS,
    _KNOWN_DASHBOARD_STATUSES,
    dashboard_request_duration_seconds,
    dashboard_requests_total,
)

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from starlette.types import ASGIApp, Receive, Scope, Send

    Message = MutableMapping[str, object]


_SENSITIVE_PATHS: frozenset[str] = frozenset({"/audit", "/redactions"})
"""Routes that bucket under the ``"<sensitive>"`` label."""


def bucket_path(path: str) -> str:
    """Map a request path to a closed-enum metric label.

    The function is the single source of truth for how the
    dashboard's ``path`` label is computed; the
    ``test_dashboard_metrics_*`` suite exercises every branch.
    """
    if path in _SENSITIVE_PATHS:
        return "<sensitive>"
    if path.startswith("/static/"):
        return "/static"
    if path in _KNOWN_DASHBOARD_PATHS:
        return path
    return "<other>"


def bucket_status(code: int) -> str:
    """Map an HTTP status code to a closed-enum metric label."""
    label = str(code)
    if label in _KNOWN_DASHBOARD_STATUSES:
        return label
    return "<other>"


class DashboardMetricsMiddleware:
    """ASGI middleware that records the request counter + duration
    histogram for every HTTP request the dashboard serves.

    Wraps the FastHTML / Starlette app at the outermost layer so
    even 4xx and 5xx responses emitted by the framework's exception
    handlers land on the counter — a 500 spike is the operator's
    earliest signal of a regression.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        path_label = bucket_path(path)
        status_holder: dict[str, int] = {"code": 0}

        async def _send(message: Message) -> None:
            if message.get("type") == "http.response.start":
                code = message.get("status", 0)
                if isinstance(code, int):
                    status_holder["code"] = code
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, _send)
        finally:
            duration = time.perf_counter() - start
            status_label = bucket_status(status_holder["code"] or 500)
            dashboard_requests_total.labels(
                path=path_label,
                status=status_label,
            ).inc()
            dashboard_request_duration_seconds.observe(duration)


async def metrics_endpoint() -> tuple[bytes, str]:
    """Render the Prometheus exposition format for the dashboard.

    Imports ``prometheus_client`` lazily so a base install without
    the ``[obs]`` extra still imports the module cleanly. When
    ``prometheus_client`` is not installed, returns an empty body
    plus the standard text content-type — operators see an empty
    page rather than a 500."""
    try:
        from prometheus_client import (
            CONTENT_TYPE_LATEST,
            generate_latest,
        )
    except ImportError:
        return b"", "text/plain; charset=utf-8"
    return generate_latest(), CONTENT_TYPE_LATEST


# Bridging callable so the dashboard's request handler can be a
# plain async function (no FastHTML decorator needed for /metrics
# specifically — the route is operator-internal observability).
async def render_metrics_response() -> tuple[bytes, str]:
    return await metrics_endpoint()


__all__ = [
    "DashboardMetricsMiddleware",
    "bucket_path",
    "bucket_status",
    "metrics_endpoint",
    "render_metrics_response",
]
