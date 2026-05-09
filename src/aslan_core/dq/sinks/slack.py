"""Slack sink — POST to an incoming webhook URL.

Format the alert as a Slack ``attachments`` payload with severity-
mapped colour:

  * ``critical`` / ``error``  → ``danger`` (red)
  * ``warn``                  → ``warning`` (yellow)
  * ``info``                  → ``good`` (green) — distinct from
                                Slack's default neutral so info-level
                                alerts (e.g. weekly_scorecard) are
                                visually distinguishable.

The text body carries the rule name + severity, plus a JSON-formatted
``fields`` block with the payload's top-level keys (clipped to 10 to
respect Slack's per-attachment field cap).

httpx.AsyncClient with a 5-second timeout, same as the GlitchTip sink.
On non-2xx response, raise ``RuntimeError`` so the dispatcher records
``status='failed'`` with the body text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from aslan_core.dq.sinks._base import SinkNotConfigured

if TYPE_CHECKING:
    from aslan_core.config import Settings


_SEVERITY_TO_SLACK_COLOR: dict[str, str] = {
    "critical": "danger",
    "error": "danger",
    "warn": "warning",
    "info": "good",
}


def _format_fields(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Render payload's first 10 top-level keys as Slack fields.

    Long string values get truncated to 200 chars so a stray multi-KB
    payload key doesn't drown the channel.
    """
    fields: list[dict[str, Any]] = []
    for k, v in list(payload.items())[:10]:
        # str(dict) and str(list) render structured values legibly enough
        # for a Slack field; any other scalar gets the same str() coerce.
        value_text = str(v)
        if len(value_text) > 200:
            value_text = value_text[:200] + "…"
        fields.append({"title": str(k), "value": value_text, "short": len(value_text) < 40})
    return fields


class SlackSink:
    """POST one alert payload to a Slack incoming webhook URL."""

    def __init__(
        self,
        *,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client

    async def deliver(
        self,
        payload: dict[str, Any],
        severity: str,
        rule_name: str,
    ) -> bool:
        if self._settings.audit_slack_webhook_url is None:
            raise SinkNotConfigured("audit_slack_webhook_url not configured")
        webhook = self._settings.audit_slack_webhook_url.get_secret_value()
        color = _SEVERITY_TO_SLACK_COLOR.get(severity, "warning")

        slack_payload: dict[str, Any] = {
            "text": f"*[ASLAN AUDIT]* `{rule_name}` — *{severity}*",
            "attachments": [
                {
                    "color": color,
                    "title": rule_name,
                    "fields": _format_fields(payload),
                    "fallback": f"[ASLAN AUDIT] {rule_name} ({severity})",
                }
            ],
        }
        if self._client is not None:
            response = await self._client.post(webhook, json=slack_payload)
        else:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(webhook, json=slack_payload)
        if response.status_code >= 400:
            raise RuntimeError(
                f"slack webhook POST returned {response.status_code}: {response.text[:200]}"
            )
        return True


__all__ = ["SlackSink"]
