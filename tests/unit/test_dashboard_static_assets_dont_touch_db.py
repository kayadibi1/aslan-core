"""Static asset handler is pure — no DB, no Redis, no I/O outside
the package.

Spec §6.3 finding 6: ``serve_static(name)`` MUST NOT touch the DB
session, the Redis client, the request query string, or the
request body. That isolation is what lets the dashboard answer
asset requests with the same latency profile whether the
testcontainer DB is up or not — important for the deploy-
verification path where the operator hits ``/`` to confirm the
process is alive before any backend has come up.

The tests here exercise the handler with the dashboard app
*deliberately not configured* (no session_factory, no redis
client) and assert it still serves a 200. Any code path that
touched ``get_session_factory()`` or ``get_redis_client()`` would
raise ``RuntimeError`` and surface as a 500.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from aslan_core.dashboard.static import HTMX_SRI, serve_static


def test_serve_static_returns_dashboard_css_with_text_css_content_type() -> None:
    response = serve_static("dashboard.css")
    assert response.status_code == 200
    assert response.media_type == "text/css; charset=utf-8"
    assert response.headers["cache-control"] == "public, max-age=86400"


def test_serve_static_returns_htmx_with_immutable_cache_directive() -> None:
    response = serve_static("htmx.min.js")
    assert response.status_code == 200
    assert response.media_type == "application/javascript; charset=utf-8"
    assert "immutable" in response.headers["cache-control"]


def test_serve_static_returns_favicon_with_image_x_icon() -> None:
    response = serve_static("favicon.ico")
    assert response.status_code == 200
    assert response.media_type == "image/x-icon"


@pytest.mark.parametrize(
    "name",
    [
        "../etc/passwd",
        "../",
        "..",
        "..\\windows",
        "subdir/dashboard.css",
        "htmx.min.js.map",
        "htmx.min.js.sha256",
        "dashboard.css.bak",
        "",
    ],
)
def test_serve_static_rejects_anything_outside_allowlist(name: str) -> None:
    """Even if a future route accidentally passes a fabricated name,
    the handler maps against a literal dict — traversal sequences
    cannot reach ``importlib.resources``.

    Note ``htmx.min.js.sha256`` is the SRI sidecar; treating it as
    a public asset would let a network observer correlate the SRI
    metadata with the JS bytes. The sidecar is internal-only."""
    response = serve_static(name)
    assert response.status_code == 404


def test_htmx_sri_is_base64_sha256_of_file_bytes() -> None:
    """Cross-check: the SRI string the base template references is
    the same SHA-256 the handler verifies. A divergence here means
    the static module loaded the file and the sidecar disagree —
    it would already have raised at module-import time, so this is
    a reachability assertion."""
    assert HTMX_SRI.startswith("sha256-")
    actual_b64 = HTMX_SRI.removeprefix("sha256-")
    # Decode + re-hash the file via the public handler path.
    response = serve_static("htmx.min.js")
    assert response.status_code == 200
    digest = hashlib.sha256(response.body).digest()
    assert actual_b64 == base64.b64encode(digest).decode("ascii")
