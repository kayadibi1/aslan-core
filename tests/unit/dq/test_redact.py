"""Unit tests for dq.sync_log._redact (TC kimlik PII redaction)."""

from __future__ import annotations

from aslan_core.dq.sync_log import _TC_KIMLIK_RE, _redact


def test_redact_strips_11_digit_sequences() -> None:
    text = "Customer 12345678901 had error processing"
    result = _redact(text)
    assert "12345678901" not in result
    assert "[REDACTED-TC-KIMLIK]" in result


def test_redact_preserves_other_numbers() -> None:
    text = "Order #1234 cost $567.89 on 2026-05-09"
    result = _redact(text)
    assert "1234" in result
    assert "567" in result


def test_redact_handles_multiple_kimliks() -> None:
    text = "User 12345678901 and User 98765432109 collided"
    result = _redact(text)
    assert "12345678901" not in result
    assert "98765432109" not in result
    assert result.count("[REDACTED-TC-KIMLIK]") == 2


def test_redact_word_boundary() -> None:
    """11-digit sequences embedded in larger numerics should NOT redact
    (those aren't valid TC kimliks anyway). Only word-bounded matches."""
    text = "BigNumber123456789012345 should not match the embedded 11"
    result = _redact(text)
    assert "BigNumber123456789012345" in result


def test_tc_kimlik_re_pattern() -> None:
    assert _TC_KIMLIK_RE.pattern == r"\b\d{11}\b"


def test_redact_empty_string() -> None:
    assert _redact("") == ""
