"""Observability public API for aslan-core.

The Sentry / OpenTelemetry / Prometheus integrations live behind the
``aslan-core[obs]`` install extra. Calling ``setup_sentry(None)`` /
``setup_tracing(None)`` is always a no-op and does NOT require the
extra; the heavy imports are deferred until first non-``None`` call so
a base-install consumer can ``from aslan_core.observability import
setup_sentry`` without ``ImportError``.

The :mod:`aslan_core.observability.metrics` submodule similarly defers
``prometheus_client`` import until a counter / histogram is actually
incremented or observed.
"""

from __future__ import annotations

from aslan_core.observability.sentry import setup_sentry
from aslan_core.observability.tracing import setup_tracing, traced

__all__ = [
    "setup_sentry",
    "setup_tracing",
    "traced",
]
