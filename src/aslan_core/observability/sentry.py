"""Sentry SDK integration with a four-layer PII / secret redactor.

Public API:

* :func:`setup_sentry` — initialize the Sentry SDK. No-op when ``dsn``
  is ``None``. The ``sentry-sdk`` import is *lazy* (inside the function
  body) so a consumer that does not install ``aslan-core[obs]`` can
  still call ``setup_sentry(None)`` without an ``ImportError``.

The redactor (:func:`_redact`) is pure-Python and does not depend on
``sentry-sdk`` — it is exercised directly in unit tests.

Redaction layers (codex F4 + F5 + F8, 2026-04-29):

  1. **Name-based secret redaction** — keys matching ``password``,
     ``token``, ``secret``, ``api_key``, ``credentials``, ``cookie``,
     or ``authorization`` (case-insensitive) are always redacted.
  2. **Exact-name filing payload redaction** — keys named exactly
     ``primary_bytes``, ``xbrl_bytes``, ``body_text``,
     ``attachment_bytes``, or ``att_bytes`` are redacted regardless
     of size.
  3. **Sibling-aware payload redaction** — generic keys ``bytes``,
     ``data``, ``content``, or ``body`` are redacted only when their
     parent dict also contains a ``mime`` / ``mime_type`` /
     ``content_type`` sibling. Catches the AttachmentIn shape and
     HTTP-response-with-content-type shapes while leaving plain
     ``body`` / ``content`` fields untouched.
  4. **Size-based defense-in-depth** — any remaining ``bytes`` /
     ``bytearray`` value over 8 KB is replaced with a length marker.

The exact-name and sibling-aware lists are *disjoint* (codex F8): the
prior version had ``body`` and ``content`` in both, which over-redacted
plain HTTP body fields. ``body`` and ``content`` only ever fall under
the sibling-aware rule now.
"""

from __future__ import annotations

import re
from typing import Any

# ── Redaction tables ─────────────────────────────────────────────────

_SECRET_KEY_PATTERN = re.compile(
    r"(password|token|secret|api[_-]?key|credentials|cookie|authorization)",
    re.IGNORECASE,
)

# Layer 2: exact-name filing payload keys — redacted regardless of size.
# The list is intentionally narrow so a generic ``body`` / ``content``
# (e.g. HTTP request body) is NOT auto-redacted under it; those fall to
# the sibling-aware rule.
_FILING_PAYLOAD_KEYS = frozenset(
    {
        "primary_bytes",
        "xbrl_bytes",
        "body_text",
        "attachment_bytes",
        "att_bytes",
    }
)

# Layer 3: presence of any of these keys marks a dict as filing-payload
# shape (AttachmentIn: ``{"bytes": b"...", "mime": "...", "filename": ...}``).
_MIME_SIBLING_KEYS = frozenset({"mime", "mime_type", "content_type"})
_AMBIGUOUS_PAYLOAD_KEYS = frozenset({"bytes", "data", "content", "body"})

# Layer 4: defense-in-depth size cap (bytes).
_SIZE_CAP_BYTES = 8192


def _is_payload_dict(d: dict[Any, Any]) -> bool:
    """True iff ``d`` has at least one ambiguous-payload key AND a mime
    sibling (the AttachmentIn / file-record shape)."""
    keys = {str(k) for k in d}
    return bool(keys & _AMBIGUOUS_PAYLOAD_KEYS) and bool(keys & _MIME_SIBLING_KEYS)


def _redact(obj: Any) -> Any:
    """Walk an event payload and apply the four redaction layers.

    Recurses into dicts and lists. Returns a new structure with the
    same shape; original input is not mutated. Idempotent.
    """
    if isinstance(obj, dict):
        is_payload_shape = _is_payload_dict(obj)
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            kstr = str(k)
            # Layer 1: secret-key names.
            if _SECRET_KEY_PATTERN.search(kstr):
                out[k] = "[Redacted: secret]"
                continue
            # Layer 2: exact-name filing payload keys.
            if kstr in _FILING_PAYLOAD_KEYS:
                size = _safe_len(v)
                out[k] = (
                    f"[Redacted: filing payload ({size} bytes)]"
                    if size is not None
                    else "[Redacted: filing payload]"
                )
                continue
            # Layer 3: sibling-aware redaction inside a payload-shape dict.
            if is_payload_shape and kstr in _AMBIGUOUS_PAYLOAD_KEYS:
                size = _safe_len(v)
                out[k] = (
                    f"[Redacted: payload ({size} bytes, sibling: mime)]"
                    if size is not None
                    else "[Redacted: payload]"
                )
                continue
            out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    # Layer 4: size cap on naked bytes/bytearray values.
    if isinstance(obj, bytes | bytearray) and len(obj) > _SIZE_CAP_BYTES:
        return f"[Redacted: {len(obj)} bytes]"
    return obj


def _safe_len(v: Any) -> int | None:
    if isinstance(v, bytes | bytearray | str):
        return len(v)
    return None


def _before_send(event: Any, hint: Any) -> Any:
    """Sentry ``before_send`` hook. Walks the event through :func:`_redact`."""
    return _redact(event)


def setup_sentry(
    dsn: str | None = None,
    *,
    environment: str = "local",
    sample_rate: float = 1.0,
    traces_sample_rate: float = 0.1,
) -> None:
    """Initialize the Sentry SDK with the four-layer PII redactor wired
    as ``before_send``.

    No-op when ``dsn`` is ``None`` — consumers without the
    ``aslan-core[obs]`` extra can call ``setup_sentry(None)`` without
    an ``ImportError``. The ``sentry-sdk`` import is lazy.

    Domain errors that represent expected control flow
    (``EntityMergeRequired``, ``IdentifierConflict``,
    ``DocumentNotFound``) are added to ``ignore_errors`` so they do
    not flood the Sentry UI.
    """
    if dsn is None:
        return
    # Lazy import: only required when DSN is configured.
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        sample_rate=sample_rate,
        traces_sample_rate=traces_sample_rate,
        before_send=_before_send,
        ignore_errors=[
            "aslan_core.errors.EntityMergeRequired",
            "aslan_core.errors.IdentifierConflict",
            "aslan_core.errors.DocumentNotFound",
        ],
    )
