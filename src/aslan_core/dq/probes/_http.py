"""Proxy-aware httpx fetch helper for the KAP / MKK upstream probes.

Mirrors the proxy-routing pattern from ``crawl.src.kap.http.KAPHTTPClient``
verbatim (Settings.proxy_url -> httpx.AsyncClient(proxy=...)) but
without the heavy lifecycle: the probes do one read-only GET per
sweep, no raw store, no Redis cache, no rate-limit budget. This
helper is the minimal surface that satisfies the workspace binding
constraint ("KAP HTTP from any aslan service MUST honor
``KAP_PROXY_URL``") while keeping the probe code free of the heavier
ingestion-tier deps.

Reads ``KAP_PROXY_URL`` from os.environ at call time so a forgotten
env var is observable (a missing proxy on a sweep that targets KAP
will surface in the probe_detail as ``"proxy": "direct"``); the dq
operator can grep recency_observation.probe_detail for that value.

Default 10s timeout — the probe runs every 5 min, so a too-slow
upstream should fail fast rather than block the sweep on the rest of
the source list.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

DEFAULT_TIMEOUT_S = 10.0


@asynccontextmanager
async def proxy_aware_client(
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    proxy_env_var: str = "KAP_PROXY_URL",
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Yield ``(client, proxy_label)`` where ``proxy_label`` is the
    proxy URL or the literal string ``"direct"``.

    The probe persists the proxy_label onto ``probe_detail`` so an
    operator inspecting an unexpectedly-stale upstream reading can
    confirm whether the request went through the rotating pool or
    direct (a misconfigured deployment that bypasses the pool will
    show ``"proxy": "direct"`` and is the canonical "why is KAP
    rate-limiting us" debug crumb).

    The default ``proxy_env_var`` is ``KAP_PROXY_URL`` so the helper
    reuses the same env-var contract as ``crawl.src.kap.http``. The
    MKK probe uses the same name — there is one rotating pool per
    deployment, not per-source.
    """
    proxy_url: str | None = os.environ.get(proxy_env_var) or None
    proxy_label = proxy_url if proxy_url is not None else "direct"
    client_kwargs: dict[str, object] = {
        "timeout": httpx.Timeout(timeout),
        "follow_redirects": True,
    }
    if proxy_url is not None:
        client_kwargs["proxy"] = proxy_url
    client = httpx.AsyncClient(**client_kwargs)  # type: ignore[arg-type]
    try:
        yield client, proxy_label
    finally:
        await client.aclose()


__all__ = ["DEFAULT_TIMEOUT_S", "proxy_aware_client"]
