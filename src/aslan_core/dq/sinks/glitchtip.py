"""GlitchTip sink — POST a Sentry-compatible event payload.

GlitchTip uses the Sentry "store" endpoint shape; the DSN is parsed
into ``(public_key, host, project_id)`` and the JSON payload is
delivered as ``POST {host}/api/{project_id}/store/`` with a
``X-Sentry-Auth`` header.

Auth header format follows Sentry's protocol envelope spec:

    Sentry sentry_version=7,sentry_key={public_key},
           sentry_client=aslan-core-dq/0.1

The sink calls httpx.AsyncClient with a 5-second timeout — alerts
are operator-facing so a slow dispatch is preferable to a stalled
loop. On non-2xx response, raises a generic ``RuntimeError`` with the
status + body so the dispatcher records the failure with diagnostics.

The payload is shipped as a Sentry-style ``message`` event, with the
rule's ``severity`` mapped onto Sentry's ``level`` (``info``/``warning``/
``error``/``fatal``) and rule + source context attached as ``tags``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from aslan_core.dq.sinks._base import SinkNotConfigured

if TYPE_CHECKING:
    from aslan_core.config import Settings


# Sentry "level" enum vs our internal severity. Sentry has no ``critical``
# slot — the convention is ``fatal``.
_SEVERITY_TO_SENTRY_LEVEL: dict[str, str] = {
    "info": "info",
    "warn": "warning",
    "error": "error",
    "critical": "fatal",
}


class GlitchTipSink:
    """POST one alert payload to a GlitchTip / Sentry-compatible endpoint."""

    def __init__(
        self,
        *,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        # Tests inject a pre-mocked httpx client. Production paths leave
        # this None and the deliver() method opens a fresh client per
        # call (the dispatch loop is one-shot drain anyway, not a hot
        # path; per-call connection setup is cheap relative to the
        # network round-trip).
        self._client = client

    @staticmethod
    def _parse_dsn(dsn: str) -> tuple[str, str, str]:
        """Parse a Sentry-format DSN into (public_key, host, project_id).

        Format: ``https://<public_key>@<host>/<project_id>``. We tolerate
        a path-prefixed host (``/api`` etc.) by treating the last path
        segment as the project_id.
        """
        parsed = urlparse(dsn)
        if not parsed.scheme or not parsed.username or not parsed.hostname:
            raise SinkNotConfigured(
                f"audit_glitchtip_dsn malformed: expected "
                f"'https://<public_key>@<host>/<project_id>', got {dsn!r}"
            )
        public_key = parsed.username
        # Project id is the last non-empty path segment.
        path_parts = [p for p in parsed.path.split("/") if p]
        if not path_parts:
            raise SinkNotConfigured(f"audit_glitchtip_dsn missing project_id segment: {dsn!r}")
        project_id = path_parts[-1]
        # Reconstruct host with scheme, port (if any), and path prefix
        # minus the trailing project_id segment.
        host_path_prefix = "/" + "/".join(path_parts[:-1]) if len(path_parts) > 1 else ""
        netloc = parsed.hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        host = f"{parsed.scheme}://{netloc}{host_path_prefix}"
        return public_key, host, project_id

    async def deliver(
        self,
        payload: dict[str, Any],
        severity: str,
        rule_name: str,
    ) -> bool:
        if self._settings.audit_glitchtip_dsn is None:
            raise SinkNotConfigured("audit_glitchtip_dsn not configured")
        dsn = self._settings.audit_glitchtip_dsn.get_secret_value()
        public_key, host, project_id = self._parse_dsn(dsn)

        endpoint = f"{host}/api/{project_id}/store/"
        sentry_level = _SEVERITY_TO_SENTRY_LEVEL.get(severity, "error")
        sentry_payload: dict[str, Any] = {
            "message": f"[{rule_name}] {severity}",
            "level": sentry_level,
            "platform": "python",
            "logger": "aslan_core.dq.alert_dispatch",
            "tags": {
                "rule": rule_name,
                "severity": severity,
                "source": str(payload.get("source", "")) if payload else "",
            },
            "extra": payload,
        }
        auth_header = (
            f"Sentry sentry_version=7,sentry_key={public_key},sentry_client=aslan-core-dq/0.1"
        )
        headers = {
            "Content-Type": "application/json",
            "X-Sentry-Auth": auth_header,
        }

        if self._client is not None:
            response = await self._client.post(endpoint, json=sentry_payload, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(endpoint, json=sentry_payload, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(
                f"glitchtip POST {endpoint} returned {response.status_code}: {response.text[:200]}"
            )
        return True


__all__ = ["GlitchTipSink"]
