"""dq alert sinks (M3) — GlitchTip / email / Slack delivery surfaces.

Each sink exposes ``async deliver(payload, severity, rule_name) -> bool``
returning ``True`` on successful delivery. A sink that cannot deliver
because the relevant config (DSN / SMTP URL / webhook URL / recipient
list) is absent raises ``SinkNotConfigured`` — the dispatcher catches
it and marks the corresponding ``audit.alert_dispatch`` row as
``status='suppressed'`` rather than ``status='failed'``. Any other
exception propagates and the dispatcher records ``status='failed'``
with the exception text in the row's ``payload``.

Public surface (used by ``aslan_core.dq.alert_dispatch``):

  * ``Sink``                 — Protocol every sink class implements
  * ``SinkNotConfigured``    — raised when a sink can't run for env reasons
  * ``GlitchTipSink``        — POST to GlitchTip / Sentry-compatible API
  * ``EmailSink``            — SMTP via stdlib smtplib
  * ``SlackSink``            — POST to a Slack incoming webhook
  * ``build_default_sinks()``— construct the three sinks from a Settings
"""

from __future__ import annotations

from aslan_core.config import Settings
from aslan_core.dq.sinks._base import Sink, SinkNotConfigured
from aslan_core.dq.sinks.email import EmailSink
from aslan_core.dq.sinks.glitchtip import GlitchTipSink
from aslan_core.dq.sinks.slack import SlackSink


def build_default_sinks(settings: Settings) -> dict[str, Sink]:
    """Construct the canonical three-sink fanout from ``Settings``.

    Returns a dict keyed by the ``audit.alert_dispatch.sink`` value
    (``glitchtip``, ``email``, ``slack``) so the dispatcher can route
    each pending row to the matching implementation. Sinks whose env
    config is absent are still included — they raise
    ``SinkNotConfigured`` at deliver-time, which the dispatcher
    translates to ``status='suppressed'``.
    """
    return {
        "glitchtip": GlitchTipSink(settings=settings),
        "email": EmailSink(settings=settings),
        "slack": SlackSink(settings=settings),
    }


__all__ = [
    "EmailSink",
    "GlitchTipSink",
    "Sink",
    "SinkNotConfigured",
    "SlackSink",
    "build_default_sinks",
]
