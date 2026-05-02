"""Unit tests for the dead-letter routing helpers.

Codex F19 round 8 — :func:`redis_id_increment` /
:func:`redis_id_compare` underpin the paginated XRANGE walk's cursor
advancement. Wrong arithmetic = orphan misclassification, so the
helpers are exercised pre-integration with focused unit cases.
"""

from __future__ import annotations

import pytest

from aslan_core.streams.deadletter import redis_id_compare, redis_id_increment


class TestRedisIdIncrement:
    def test_seq_bump(self) -> None:
        assert redis_id_increment("1714492800000-0") == "1714492800000-1"

    def test_seq_bump_large(self) -> None:
        assert redis_id_increment("1714492800000-12345") == "1714492800000-12346"

    def test_wraps_when_seq_at_max(self) -> None:
        max_seq = (1 << 64) - 1
        rid = f"100-{max_seq}"
        assert redis_id_increment(rid) == "101-0"

    def test_zero_zero_increments_to_zero_one(self) -> None:
        assert redis_id_increment("0-0") == "0-1"


class TestRedisIdCompare:
    def test_equal(self) -> None:
        assert redis_id_compare("1-2", "1-2") == 0

    def test_lower_ms(self) -> None:
        assert redis_id_compare("1-9999", "2-0") == -1

    def test_higher_ms(self) -> None:
        assert redis_id_compare("2-0", "1-9999") == 1

    def test_lower_seq_same_ms(self) -> None:
        assert redis_id_compare("5-1", "5-2") == -1

    def test_higher_seq_same_ms(self) -> None:
        assert redis_id_compare("5-2", "5-1") == 1

    def test_zero_zero_baseline(self) -> None:
        assert redis_id_compare("0-0", "1-0") == -1
        assert redis_id_compare("1-0", "0-0") == 1


class TestRedisIdRoundTrip:
    """The increment helper paired with the compare helper must
    advance the cursor monotonically — every increment produces an ID
    strictly greater than the input."""

    @pytest.mark.parametrize(
        "rid",
        [
            "0-0",
            "1714492800000-0",
            "1714492800000-9999",
            f"500-{(1 << 64) - 1}",  # wrap case
        ],
    )
    def test_increment_strictly_greater(self, rid: str) -> None:
        nxt = redis_id_increment(rid)
        assert redis_id_compare(rid, nxt) == -1
        assert redis_id_compare(nxt, rid) == 1
