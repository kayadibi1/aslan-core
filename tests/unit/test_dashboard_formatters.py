"""Display-only formatters for the dashboard.

Spec §8.1 contract: ``aslan_core.dashboard.formatters`` covers
duration / size / id-prefix output. The functions are pure (no
DB / Redis I/O) and stable enough that template authors can
pin output strings in their integration tests.
"""

from __future__ import annotations

import pytest

from aslan_core.dashboard.formatters import (
    format_age_s,
    format_id_prefix,
    format_size_bytes,
)

# ── format_age_s ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (-5.0, "0s"),
        (0.0, "0s"),
        (0.4, "0s"),
        (5.0, "5s"),
        (59.9, "59s"),
        (60.0, "1m 0s"),
        (192.0, "3m 12s"),
        (3599.0, "59m 59s"),
        (3600.0, "1h 0m"),
        (8000.0, "2h 13m"),
        (86_399.0, "23h 59m"),
        (86_400.0, "1d 0h"),
        (86_400.0 * 7 + 3600 * 3, "7d 3h"),
    ],
)
def test_format_age_s(seconds: float, expected: str) -> None:
    assert format_age_s(seconds) == expected


# ── format_size_bytes ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1500, "1.5 KB"),
        (1024 * 1024 - 1, "1024.0 KB"),  # boundary just below MB
        (1024 * 1024, "1.0 MB"),
        (4_300_000, "4.1 MB"),
        (1024 * 1024 * 1024, "1.0 GB"),
        (3_700_000_000, "3.4 GB"),
    ],
)
def test_format_size_bytes(size: int, expected: str) -> None:
    assert format_size_bytes(size) == expected


# ── format_id_prefix ─────────────────────────────────────────────


def test_format_id_prefix_default_truncates_at_8_chars() -> None:
    full = "abcd" * 16  # 64-char hex-like
    assert format_id_prefix(full) == "abcdabcd…"


def test_format_id_prefix_short_value_passes_through() -> None:
    assert format_id_prefix("abc") == "abc"
    assert format_id_prefix("12345678") == "12345678"


def test_format_id_prefix_custom_width() -> None:
    full = "0123456789abcdef" * 4
    assert format_id_prefix(full, width=12) == "0123456789ab…"
    assert format_id_prefix(full, width=4) == "0123…"
