"""Unit tests for ``aslan_core.observability.sentry``.

Covers the four-layer redactor (codex F4 + F5 + F8, 2026-04-29):

  1. Name-based secret redaction (password / token / secret / api_key /
     credentials / cookie / authorization).
  2. Exact-name filing payload redaction (primary_bytes, xbrl_bytes,
     body_text, attachment_bytes, att_bytes) — regardless of size.
  3. Sibling-aware payload redaction — generic ``bytes`` / ``data`` /
     ``content`` / ``body`` keys redacted only when their parent dict
     also has a ``mime`` / ``mime_type`` / ``content_type`` sibling.
  4. Size-based defense-in-depth — any remaining ``bytes`` /
     ``bytearray`` over 8 KB is replaced with a length marker.

The disjoint-ness of layers 2 and 3 is part of the contract: a plain
``body`` field WITHOUT a mime sibling must NOT be redacted (otherwise
HTTP request bodies and unrelated event fields get over-redacted).
"""

from __future__ import annotations


def test_setup_sentry_with_none_dsn_is_noop() -> None:
    """No-op path must work without the [obs] extra installed."""
    from aslan_core.observability import setup_sentry

    setup_sentry(None)  # must not raise


def test_redact_strips_secret_keys() -> None:
    from aslan_core.observability.sentry import _redact

    out = _redact(
        {
            "db_password": "x",
            "user": "y",
            "api_key": "z",
            "token": "t",
            "AUTHORIZATION": "Bearer abc",
            "Cookie": "session=...",
        }
    )
    assert "Redacted" in str(out["db_password"])
    assert "Redacted" in str(out["api_key"])
    assert "Redacted" in str(out["token"])
    assert "Redacted" in str(out["AUTHORIZATION"])
    assert "Redacted" in str(out["Cookie"])
    assert out["user"] == "y"


def test_redact_caps_large_bytes_size_only() -> None:
    """Defense-in-depth: large bytes value under an unknown key still
    redacted by size cap."""
    from aslan_core.observability.sentry import _redact

    big = b"x" * 100_000
    out = _redact({"some_field": big})
    assert "Redacted" in str(out["some_field"])
    # Small unrelated bytes pass through.
    out2 = _redact({"some_field": b"x" * 10})
    assert out2["some_field"] == b"x" * 10


def test_redact_strips_small_filing_payload_by_name() -> None:
    """Codex F4 — exact-name filing keys redacted regardless of size."""
    from aslan_core.observability.sentry import _redact

    small = b"<html>tiny but private filing content</html>"
    assert len(small) < 8192
    out = _redact({"primary_bytes": small})
    assert "Redacted" in str(out["primary_bytes"])

    out = _redact({"body_text": "small body string with PII"})
    assert "Redacted" in str(out["body_text"])
    assert "PII" not in out["body_text"]

    out = _redact({"xbrl_bytes": b"small XBRL payload"})
    assert "Redacted" in str(out["xbrl_bytes"])

    out = _redact({"attachment_bytes": b"a"})
    assert "Redacted" in str(out["attachment_bytes"])

    out = _redact({"att_bytes": b"a"})
    assert "Redacted" in str(out["att_bytes"])


def test_redact_walks_nested_structures() -> None:
    """A primary_bytes nested deep inside an event payload is still
    redacted — the redactor walks dicts and lists recursively."""
    from aslan_core.observability.sentry import _redact

    event = {
        "request": {
            "data": {
                "filing": {
                    "primary_bytes": b"nested small filing",
                    "title": "Public",
                }
            }
        }
    }
    out = _redact(event)
    assert "Redacted" in str(out["request"]["data"]["filing"]["primary_bytes"])
    assert out["request"]["data"]["filing"]["title"] == "Public"


def test_redact_sibling_aware_attachment_payload() -> None:
    """Codex F5 — AttachmentIn shape: generic ``bytes`` key paired with
    a ``mime`` sibling is redacted regardless of size."""
    from aslan_core.observability.sentry import _redact

    attachment = {
        "bytes": b"private exhibit content",
        "mime": "application/pdf",
        "filename": "exhibit.pdf",
    }
    out = _redact(attachment)
    assert "Redacted" in str(out["bytes"])
    assert out["mime"] == "application/pdf"
    assert out["filename"] == "exhibit.pdf"

    # content_type alias also triggers sibling-aware redaction.
    out = _redact({"bytes": b"x", "content_type": "text/html"})
    assert "Redacted" in str(out["bytes"])

    # mime_type alias also triggers.
    out = _redact({"data": b"x", "mime_type": "image/png"})
    assert "Redacted" in str(out["data"])

    # Sibling-aware applies to ``content`` and ``body`` too.
    out = _redact({"content": b"foo", "mime": "text/plain"})
    assert "Redacted" in str(out["content"])

    out = _redact({"body": "<html/>", "content_type": "text/html"})
    assert "Redacted" in str(out["body"])


def test_redact_no_mime_sibling_keeps_generic_payload_keys_untouched() -> None:
    """Codex F8 — disjoint-layer test. ``body`` and ``content`` are NOT
    in the exact-name list. Without a mime sibling they pass through
    unchanged — otherwise HTTP request bodies and unrelated error
    payloads would be silently over-redacted."""
    from aslan_core.observability.sentry import _redact

    # Plain `body` field (e.g. an HTTP error message body) — no mime.
    out = _redact({"body": "user-facing error", "name": "X"})
    assert out["body"] == "user-facing error"

    # Plain `content` field — no mime.
    out = _redact({"content": "some content", "title": "X"})
    assert out["content"] == "some content"

    # Generic `bytes` field without mime — passes through (small).
    out = _redact({"bytes": b"some-other-thing", "name": "not a payload"})
    assert out["bytes"] == b"some-other-thing"

    # Generic `data` field without mime — passes through (small).
    out = _redact({"data": "some data", "label": "X"})
    assert out["data"] == "some data"


def test_redact_sibling_aware_inside_attachments_list() -> None:
    """Each list element is independently sibling-aware — a list of
    AttachmentIn-shaped dicts gets each ``bytes`` redacted."""
    from aslan_core.observability.sentry import _redact

    out = _redact(
        {
            "attachments": [
                {"bytes": b"a1", "mime": "application/pdf", "filename": "a.pdf"},
                {"bytes": b"a2", "mime": "text/html", "filename": "b.html"},
            ]
        }
    )
    for att in out["attachments"]:
        assert "Redacted" in str(att["bytes"])


def test_redact_preserves_non_dict_non_list_values() -> None:
    """Scalars at the top level (None, bool, int, small bytes, plain
    str) are preserved unchanged."""
    from aslan_core.observability.sentry import _redact

    assert _redact(None) is None
    assert _redact(True) is True
    assert _redact(42) == 42
    assert _redact("hello") == "hello"
    # Top-level bytes under the cap pass through as-is.
    assert _redact(b"hi") == b"hi"
