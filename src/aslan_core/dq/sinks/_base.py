"""Shared types for the dq alert sinks package.

Lives in a private module so the per-sink files can import
``SinkNotConfigured`` without triggering the package ``__init__``
import cycle (``__init__`` imports the per-sink classes, the per-sink
classes import ``SinkNotConfigured``).
"""

from __future__ import annotations

from typing import Protocol


class SinkNotConfigured(RuntimeError):
    """A sink was asked to deliver while its env config is absent.

    The dispatcher (``alert_dispatch.dispatch_pending``) treats this as
    a non-error: the row is stamped ``status='suppressed'`` with the
    exception's str() copied into the row payload. Use it instead of
    silently dropping — operators must see in /dq that an alert was
    routed to a sink that wasn't wired.
    """


class Sink(Protocol):
    """Async sink delivering one alert payload.

    The ``payload`` arg is the JSON dict already persisted to
    ``audit.alert_dispatch.payload``; ``severity`` is the rule's
    severity (``info``/``warn``/``error``/``critical``); ``rule_name``
    is the canonical key that fired (e.g. ``recency_sla_breach``).

    Implementations:
      * Return ``True`` on confirmed delivery.
      * Raise ``SinkNotConfigured`` when env config is missing.
      * Raise any other ``Exception`` to signal a transient/permanent
        failure the dispatcher should record as ``status='failed'``.
    """

    async def deliver(
        self,
        payload: dict[str, object],
        severity: str,
        rule_name: str,
    ) -> bool: ...


__all__ = ["Sink", "SinkNotConfigured"]
