"""Unit tests — ``_compute_batch_hash`` forensic-commitment contract
(codex Batch 3 F2, 2026-04-29).

The batch hash recorded in ``audit.events.metadata.batch_payload_hash``
must BIND each payload to its ``(series_id, ts, as_of)`` key. The
previous shape sorted only payload-hash strings, so two batches that
swapped payloads across keys produced the same digest — a forensic-
commitment hole because the audit hash was supposed to detect tampering
but couldn't see a key/payload swap.

These tests are pure (no DB) so they run in milliseconds and can't be
broken by integration-test environmental drift.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aslan_core.schemas.timeseries import ObservationIn
from aslan_core.timeseries.writer import _compute_batch_hash


def _ts(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


def test_batch_hash_changes_when_payloads_swapped_across_keys() -> None:
    """Codex Batch 3 F2: hash MUST differ when payload values swap
    between two keys (same series, distinct ts/as_of)."""
    obs_a = [
        ObservationIn(ts=_ts(1), as_of=_ts(2), value=1.0),
        ObservationIn(ts=_ts(3), as_of=_ts(4), value=2.0),
    ]
    h_a = _compute_batch_hash(series_id=42, deduped=obs_a)

    # Swap values between the two ObservationIn objects (keys
    # unchanged): row at (ts=_ts(1), as_of=_ts(2)) now carries
    # value=2.0, row at (ts=_ts(3), as_of=_ts(4)) now carries
    # value=1.0. With the previous sorted-hashes-only shape this
    # would collide; with key-binding it must differ.
    obs_b = [
        ObservationIn(ts=_ts(1), as_of=_ts(2), value=2.0),
        ObservationIn(ts=_ts(3), as_of=_ts(4), value=1.0),
    ]
    h_b = _compute_batch_hash(series_id=42, deduped=obs_b)

    assert h_a != h_b, (
        "batch hash must change when payloads swap across keys; "
        "previous sorted-hashes-only shape produced a collision"
    )


def test_batch_hash_independent_of_input_ordering() -> None:
    """Codex Batch 3 F2: deterministic regardless of input order. Same
    rows in a different order produce the same digest because we sort
    by ``(ts, as_of)`` before hashing."""
    obs_forward = [
        ObservationIn(ts=_ts(1), as_of=_ts(2), value=1.0),
        ObservationIn(ts=_ts(3), as_of=_ts(4), value=2.0),
        ObservationIn(ts=_ts(5), as_of=_ts(6), value=3.0),
    ]
    obs_reversed = list(reversed(obs_forward))
    assert _compute_batch_hash(7, obs_forward) == _compute_batch_hash(7, obs_reversed)


def test_batch_hash_changes_when_series_id_changes() -> None:
    """Codex Batch 3 F2: ``series_id`` is part of the digest input —
    two series with byte-identical observations produce different
    digests."""
    obs = [
        ObservationIn(ts=_ts(1), as_of=_ts(2), value=1.0),
        ObservationIn(ts=_ts(3), as_of=_ts(4), value=2.0),
    ]
    h_a = _compute_batch_hash(series_id=1, deduped=obs)
    h_b = _compute_batch_hash(series_id=2, deduped=obs)
    assert h_a != h_b


def test_batch_hash_returns_64_char_hex() -> None:
    """Codex Batch 3 F2: SHA-256 hex digest = 64 lowercase hex chars."""
    obs = [ObservationIn(ts=_ts(1), as_of=_ts(2), value=1.0)]
    h = _compute_batch_hash(series_id=1, deduped=obs)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_batch_hash_changes_when_value_changes() -> None:
    """Sanity: same key, different value → different digest."""
    obs_a = [ObservationIn(ts=_ts(1), as_of=_ts(2), value=1.0)]
    obs_b = [ObservationIn(ts=_ts(1), as_of=_ts(2), value=2.0)]
    assert _compute_batch_hash(1, obs_a) != _compute_batch_hash(1, obs_b)


def test_batch_hash_changes_when_ts_changes() -> None:
    """Sanity: same value, different ts → different digest (the key is
    part of the canonical sequence)."""
    obs_a = [ObservationIn(ts=_ts(1), as_of=_ts(2), value=1.0)]
    obs_b = [ObservationIn(ts=_ts(2), as_of=_ts(2), value=1.0)]
    assert _compute_batch_hash(1, obs_a) != _compute_batch_hash(1, obs_b)
