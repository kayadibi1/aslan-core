"""Static asset serving — Task 11.

Spec §6.3 finding 6: the dashboard serves a small fixed allowlist of
static files. The handler:

  * Maps the request path against a literal three-element allowlist
    (no path traversal, no glob, no DB session, no Redis client).
  * Reads bytes via ``importlib.resources`` so the lookup respects
    the package boundary — escapes like ``../etc/passwd`` cannot
    reach outside ``aslan_core.dashboard.assets``.
  * Returns bytes + Content-Type + Cache-Control headers.

The route's blast-radius is bounded by the allowlist. A future
attempt to serve a fourth file requires a code change here AND a
migration of the SRI hash file (for any new JS).

htmx 1.9.12 is vendored as ``assets/htmx.min.js``. The SRI hash is
recorded in ``assets/htmx.min.js.sha256`` (hex digest, lowercase, one
line, no trailing newline guarantee). The base template's
``<script>`` tag references ``integrity="sha256-<base64>"`` — the
base64 form is computed at module-import time so a future
re-vendor only requires updating the file + its sha256 sidecar.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from importlib import resources
from typing import Final

from starlette.responses import Response

# ── Allowlist + content-type map ─────────────────────────────────


# Literal three-element allowlist. The keys are the path-suffixes the
# HTTP routes accept; values carry the Content-Type and Cache-Control.
# Any future fourth asset requires editing this dict — there is no
# directory walk or glob.
_ALLOWLIST: Final[dict[str, tuple[str, str]]] = {
    "dashboard.css": ("text/css; charset=utf-8", "public, max-age=86400"),
    "htmx.min.js": (
        "application/javascript; charset=utf-8",
        "public, max-age=86400, immutable",
    ),
    "favicon.ico": ("image/x-icon", "public, max-age=86400"),
}


# ── SRI hash for the vendored htmx ───────────────────────────────


def _read_asset_bytes(name: str) -> bytes:
    """Read an asset's bytes via importlib.resources. Raises
    ``FileNotFoundError`` if the asset is missing (caught by
    ``serve_static`` and surfaced as a 404)."""
    return (resources.files("aslan_core.dashboard.assets") / name).read_bytes()


def _htmx_sri_b64() -> str:
    """Read the SHA-256 hex sidecar and verify it matches the actual
    file. Returns the base64 SRI value for the ``integrity`` attribute.

    The sidecar is a backstop against silent corruption: if a future
    re-vendor changes the JS but leaves the hash, the module-import
    fails loudly here. Operators who want to update htmx must touch
    both files.
    """
    js_bytes = _read_asset_bytes("htmx.min.js")
    sidecar = _read_asset_bytes("htmx.min.js.sha256").decode("ascii").strip()
    actual = hashlib.sha256(js_bytes).hexdigest()
    if sidecar != actual:
        raise RuntimeError(
            "htmx asset SHA-256 sidecar does not match file. "
            f"sidecar={sidecar!r} actual={actual!r}. "
            "Update src/aslan_core/dashboard/assets/htmx.min.js.sha256 "
            "or re-vendor htmx.min.js."
        )
    digest_bytes = binascii.unhexlify(actual)
    return base64.b64encode(digest_bytes).decode("ascii")


HTMX_SRI: Final[str] = f"sha256-{_htmx_sri_b64()}"
"""SRI ``integrity`` attribute value for the vendored htmx file."""


# ── Handler ──────────────────────────────────────────────────────


def serve_static(name: str) -> Response:
    """Serve one of the allowlisted assets. ``name`` is the literal
    file segment (e.g. ``dashboard.css``); path traversal cannot
    enter this function because the route table maps fixed paths
    (``/static/dashboard.css``) to fixed names.

    Returns a 404 ``Response`` when the name is not in the
    allowlist OR the asset is missing on disk — the dashboard MUST
    NOT fail the whole app start because a CSS file got reordered.
    """
    if name not in _ALLOWLIST:
        return Response(status_code=404)
    content_type, cache_control = _ALLOWLIST[name]
    try:
        body = _read_asset_bytes(name)
    except FileNotFoundError:
        return Response(status_code=404)
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": cache_control},
    )


__all__ = ["HTMX_SRI", "serve_static"]
