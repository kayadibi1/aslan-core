"""Display-only formatters for the v0.6.0 dashboard.

Spec §6.3 + §8.1: these are pure functions over typed VM fields —
no DB access, no Redis access, no I/O. They produce strings the
page templates interpolate verbatim.

The only formatter that touches a forbidden surface is the
``format_id`` truncation for UUIDs / large integers — operators see
short prefixes by default to keep the table readable, and a future
"copy full id" htmx interaction (Task 11+) reveals the full value
on demand.
"""

from __future__ import annotations


def format_age_s(seconds: float) -> str:
    """Format a positive duration in seconds as a human-readable
    age. Negative inputs are clamped to ``0s``. The ladder is:

      ``< 60s`` → ``"5s"``
      ``< 60m`` → ``"3m 12s"``
      ``< 24h`` → ``"2h 14m"``
      ``≥ 24h`` → ``"7d 3h"``
    """
    if seconds <= 0:
        return "0s"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m {s}s"
    if seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{d}d {h}h"


def format_size_bytes(n: int) -> str:
    """Format a non-negative byte count as ``"123 B"`` / ``"4.2 KB"``
    / ``"1.7 MB"`` / ``"3.4 GB"``. Powers-of-1024."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def format_id_prefix(value: str, *, width: int = 8) -> str:
    """Truncate an identifier (UUID, hex hash, etc) to the first
    ``width`` characters with an ellipsis. Catches the case where
    a 64-char sha256 dominates an outbox row visually."""
    if len(value) <= width:
        return value
    return f"{value[:width]}…"


__all__ = ["format_age_s", "format_id_prefix", "format_size_bytes"]
