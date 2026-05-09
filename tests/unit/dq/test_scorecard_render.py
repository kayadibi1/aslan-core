"""Unit tests for dq.scorecard render helpers + week-helper.

The render helpers are pure functions over ``ScorecardRow`` lists —
no DB needed. Tests cover:

  * ``latest_week_start`` returns the previous Monday for any
    weekday-of-call, and the *current* week's Monday on Sunday calls
    (cron firing at 23:55 Sunday UTC summarising the just-ended week).

  * ``render_email`` returns a tuple with the spec-mandated subject
    format and an HTML body containing the metric rows + colour-coded
    status cells + the top-line "X% pass" summary.

  * ``render_html`` returns a standalone document with doctype.

  * ``render_text_fallback`` returns a plaintext body with the
    metric / target / actual / status / notes columns aligned.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from aslan_core.dq import scorecard


def _row(metric: str, status: scorecard.ScorecardStatus = "pass") -> scorecard.ScorecardRow:
    return scorecard.ScorecardRow(
        metric_name=metric,
        target=">= 99%",
        actual="99.5%",
        status=status,
        notes="n=42",
    )


def test_latest_week_start_sunday_returns_current_week_monday() -> None:
    """Sunday cron fire (23:55 UTC) — the just-ended week is Mon-Sun
    ending today; week_start is six days back."""
    sunday = datetime(2026, 5, 3, 23, 55, tzinfo=UTC)  # 2026-05-03 is Sun
    assert sunday.isoweekday() == 7
    assert scorecard.latest_week_start(sunday) == date(2026, 4, 27)


def test_latest_week_start_monday_returns_previous_week_monday() -> None:
    """Monday call returns the Monday of the just-ended week — Sun
    yesterday closed week starting 7 days ago."""
    monday = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)  # 2026-05-04 is Mon
    assert monday.isoweekday() == 1
    # Most-recent fully-completed week ended yesterday (Sun 2026-05-03).
    # Its Monday is 2026-04-27.
    assert scorecard.latest_week_start(monday) == date(2026, 4, 27)


def test_latest_week_start_saturday_returns_previous_week_monday() -> None:
    saturday = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    assert saturday.isoweekday() == 6
    # Most-recent fully-completed week ended Sun 2026-05-03.
    # Its Monday is 2026-04-27.
    assert scorecard.latest_week_start(saturday) == date(2026, 4, 27)


def test_render_email_subject_format() -> None:
    rows = [_row("kap_recency_p95")]
    subject, _ = scorecard.render_email(week_start=date(2026, 4, 27), rows=rows)
    assert subject == "[ASLAN AUDIT] Weekly scorecard — week of 2026-04-27"


def test_render_email_body_contains_summary_and_table() -> None:
    rows = [
        _row("kap_recency_p95", "pass"),
        _row("kap_recency_p99", "warn"),
        _row("regression_flag_open_count", "fail"),
        _row("bist_entity_coverage", "pass"),
    ]
    _, body = scorecard.render_email(week_start=date(2026, 4, 27), rows=rows)
    # Top-line "X% pass" summary
    assert "% pass" in body
    assert "50% pass" in body or "50.0% pass" in body  # 2 of 4 pass
    # Header
    assert "<h2>Aslan weekly scorecard" in body
    assert "week of 2026-04-27" in body
    # Each metric appears
    for r in rows:
        assert r.metric_name in body
    # Status cells colour-coded — at least one pass green / warn yellow / fail red
    assert "#d4edda" in body  # pass background
    assert "#fff3cd" in body  # warn background
    assert "#f8d7da" in body  # fail background
    # Status text uppercased
    assert ">PASS<" in body
    assert ">WARN<" in body
    assert ">FAIL<" in body


def test_render_email_handles_empty_rows() -> None:
    subject, body = scorecard.render_email(week_start=date(2026, 4, 27), rows=[])
    assert "2026-04-27" in subject
    assert "0 pass / 0 warn / 0 fail" in body
    assert "0% pass" in body


def test_render_html_is_standalone_document() -> None:
    rows = [_row("kap_recency_p95")]
    body = scorecard.render_html(week_start=date(2026, 4, 27), rows=rows)
    assert body.startswith("<!doctype html>")
    assert "<title>" in body
    # PDF deferred TODO must be visible to operators.
    assert "TODO" in body or "M6.1" in body


def test_render_text_fallback_has_columns() -> None:
    rows = [
        _row("kap_recency_p95", "pass"),
        _row("regression_flag_open_count", "fail"),
    ]
    body = scorecard.render_text_fallback(week_start=date(2026, 4, 27), rows=rows)
    assert "Aslan weekly scorecard — week of 2026-04-27" in body
    assert "kap_recency_p95" in body
    assert "regression_flag_open_count" in body
    assert "pass" in body
    assert "fail" in body


def test_render_email_html_escapes_dangerous_payload() -> None:
    """Notes carry user-ish content (rule_name, error strings); the
    renderer must HTML-escape them so a bracket-containing note doesn't
    smuggle markup into the email body."""
    rows = [
        scorecard.ScorecardRow(
            metric_name="kap_recency_p95",
            target=">= 99%",
            actual="99.5%",
            status="pass",
            notes="<script>alert('xss')</script>",
        ),
    ]
    _, body = scorecard.render_email(week_start=date(2026, 4, 27), rows=rows)
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_compute_rejects_non_monday_week_start() -> None:
    """compute() refuses a non-Monday week_start to keep the cron
    contract diff-stable across runs."""
    import asyncio
    from typing import cast

    from sqlalchemy.ext.asyncio import AsyncSession

    async def call() -> None:
        # Pass a stand-in None — the function raises before touching it.
        # Cast keeps mypy strict happy without an unused type:ignore.
        await scorecard.compute(
            session=cast(AsyncSession, None),
            week_start=date(2026, 4, 28),
        )

    try:
        asyncio.run(call())
        raise AssertionError("expected ValueError for non-Monday week_start")
    except ValueError as exc:
        assert "Monday" in str(exc)
