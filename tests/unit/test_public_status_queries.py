"""NG1 — unit tests for the per-source row computation.

Drives the pure functions in :mod:`aslan_core.public_status.queries`
without a database. Covers:

  * ``_humanize_age`` boundary cases (sec / min / hr / day, negative);
  * ``_freshness_pct`` SLA scaling (within / over / 2× / degenerate);
  * ``_classify_badge`` ordering (CRIT > WARN > stale > OK);
  * ``_build_row`` for the missing-recency UNKNOWN path;
  * ``_build_status`` headline averaging excludes UNKNOWN sources.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aslan_core.public_status.queries import (
    _PUBLIC_SOURCES,
    _build_row,
    _build_status,
    _classify_badge,
    _freshness_pct,
    _humanize_age,
)
from aslan_core.public_status.view_models import StatusBadge

# ── _humanize_age ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (-5.0, "0 sec ago"),
        (0.0, "0 sec ago"),
        (1.4, "1 sec ago"),
        (59.9, "59 sec ago"),
        (60.0, "1 min ago"),
        (60 * 59.9, "59 min ago"),
        (60 * 60.0, "1 hr ago"),
        (60 * 60 * 23.9, "23 hr ago"),
        (60 * 60 * 24.0, "1 d ago"),
        (60 * 60 * 24 * 7, "7 d ago"),
    ],
)
def test_humanize_age_buckets(delta: float, expected: str) -> None:
    assert _humanize_age(delta) == expected


# ── _freshness_pct ─────────────────────────────────────────────────


def test_freshness_within_sla_is_100() -> None:
    assert _freshness_pct(0, 300) == 100.0
    assert _freshness_pct(299, 300) == 100.0
    assert _freshness_pct(300, 300) == 100.0


def test_freshness_at_2x_sla_is_0() -> None:
    assert _freshness_pct(600, 300) == 0.0
    assert _freshness_pct(601, 300) == 0.0


def test_freshness_linear_between_1x_and_2x() -> None:
    # halfway between 1x and 2x sla -> 50%
    assert _freshness_pct(450, 300) == 50.0
    # 25% of the way over -> 75%
    assert _freshness_pct(375, 300) == 75.0


def test_freshness_degenerate_sla_returns_0() -> None:
    assert _freshness_pct(100, 0) == 0.0
    assert _freshness_pct(100, -1) == 0.0


# ── _classify_badge ────────────────────────────────────────────────


def test_classify_badge_ok_path() -> None:
    """Lag within SLA + observation fresh -> OK."""
    assert _classify_badge(100, 300, observation_age_seconds=60.0) == StatusBadge.OK


def test_classify_badge_warn_when_observation_stale() -> None:
    """Lag within SLA but the observation row is stale (cron not
    writing) -> WARN. 16 minutes > 15-minute threshold."""
    assert _classify_badge(100, 300, observation_age_seconds=16 * 60) == StatusBadge.WARN


def test_classify_badge_warn_when_lag_over_sla() -> None:
    """Lag past SLA but within 2x -> WARN."""
    assert _classify_badge(450, 300, observation_age_seconds=60.0) == StatusBadge.WARN


def test_classify_badge_crit_when_lag_past_2x_sla() -> None:
    """Lag at or past 2x SLA -> CRIT regardless of observation age."""
    assert _classify_badge(600, 300, observation_age_seconds=60.0) == StatusBadge.CRIT
    # CRIT wins over stale-observation WARN.
    assert _classify_badge(600, 300, observation_age_seconds=24 * 60 * 60) == StatusBadge.CRIT


def test_classify_badge_unknown_for_degenerate_sla() -> None:
    assert _classify_badge(100, 0, observation_age_seconds=60.0) == StatusBadge.UNKNOWN


# ── _build_row ─────────────────────────────────────────────────────


def test_build_row_no_recency_returns_unknown() -> None:
    row = _build_row(
        source="kap",
        recency=None,
        coverage_pct=87.5,
        now=datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
    )
    assert row.source == "kap"
    assert row.last_update_at is None
    assert row.last_update_age_label == "never"
    assert row.freshness_pct == 0.0
    assert row.coverage_pct == 87.5
    assert row.badge is StatusBadge.UNKNOWN


def test_build_row_with_recency_populates_all_fields() -> None:
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    upstream = now - timedelta(minutes=2)
    observed = now - timedelta(seconds=30)
    row = _build_row(
        source="bist",
        recency=(120, 300, upstream, observed),
        coverage_pct=99.9,
        now=now,
    )
    assert row.source == "bist"
    assert row.last_update_at == upstream
    assert row.last_update_age_label == "2 min ago"
    # 120s lag <= 300s SLA -> 100% fresh
    assert row.freshness_pct == 100.0
    assert row.coverage_pct == 99.9
    assert row.badge is StatusBadge.OK


def test_build_row_critical_path() -> None:
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    upstream = now - timedelta(hours=3)
    observed = now - timedelta(minutes=5)
    # 3-hour lag, 5-min SLA -> >2x SLA -> CRIT
    row = _build_row(
        source="evds",
        recency=(3 * 3600, 300, upstream, observed),
        coverage_pct=None,
        now=now,
    )
    assert row.badge is StatusBadge.CRIT
    assert row.freshness_pct == 0.0
    assert row.coverage_pct is None
    assert row.last_update_age_label == "3 hr ago"


# ── _build_status ──────────────────────────────────────────────────


def test_build_status_excludes_unknown_from_headline_average() -> None:
    """Only sources with an observation contribute to the headline.

    KAP at 100%, BIST at 50%, the other three UNKNOWN -> headline
    is (100+50)/2 = 75, NOT (100+50+0+0+0)/5 = 30. The UNKNOWN
    sources still surface their own UNKNOWN badge.
    """
    now = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    upstream = now - timedelta(seconds=60)
    observed = now - timedelta(seconds=10)
    # KAP fresh (100%); BIST 50% over halfway between SLA and 2*SLA.
    recency: dict[str, tuple[int, int, datetime, datetime]] = {
        "kap": (60, 300, upstream, observed),
        "bist": (5400, 3600, upstream, observed),  # 90 min vs 60 min SLA
    }
    coverage: dict[str, float | None] = {"kap": 99.0, "bist": 88.0}
    vm = _build_status(recency=recency, coverage=coverage, now=now)
    assert vm.overall_freshness_pct == 75.0
    # All five sources still produce a row.
    assert len(vm.rows) == len(_PUBLIC_SOURCES)
    by_src = {r.source: r for r in vm.rows}
    assert by_src["kap"].badge is StatusBadge.OK
    # bist 90 min vs 60 min SLA: lag > sla but lag < 2*sla -> WARN
    assert by_src["bist"].badge is StatusBadge.WARN
    assert by_src["evds"].badge is StatusBadge.UNKNOWN
    assert by_src["tefas"].badge is StatusBadge.UNKNOWN
    assert by_src["mkk"].badge is StatusBadge.UNKNOWN
    # last_updated_at is the most-recent observation across sources.
    assert vm.last_updated_at == observed


def test_build_status_no_observations_returns_zero_overall() -> None:
    now = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    vm = _build_status(recency={}, coverage={}, now=now)
    assert vm.overall_freshness_pct == 0.0
    assert vm.last_updated_at is None
    # Still surfaces 5 rows, all UNKNOWN.
    assert len(vm.rows) == len(_PUBLIC_SOURCES)
    for r in vm.rows:
        assert r.badge is StatusBadge.UNKNOWN
        assert r.freshness_pct == 0.0
        assert r.coverage_pct is None
