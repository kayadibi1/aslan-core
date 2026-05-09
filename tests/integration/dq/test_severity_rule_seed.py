"""Verify migration 0059 seeds the 9 severity rules per spec §10.3."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


# (rule_name, severity, sinks-set, throttle_seconds)
_EXPECTED: dict[str, tuple[str, frozenset[str], int]] = {
    "recency_sla_breach": ("error", frozenset({"glitchtip", "slack"}), 300),
    "recency_sla_breach_2x": ("critical", frozenset({"glitchtip", "slack"}), 60),
    "validation_pass_rate_below_95": ("warn", frozenset({"glitchtip"}), 1800),
    "validation_pass_rate_below_90": ("error", frozenset({"glitchtip", "slack"}), 600),
    "coverage_below_target": ("warn", frozenset({"glitchtip", "email"}), 3600),
    "coverage_below_90pct": ("error", frozenset({"glitchtip", "slack"}), 600),
    "bloomberg_loses_field": ("error", frozenset({"glitchtip", "email"}), 86400),
    "regression_flag_critical_entity": ("warn", frozenset({"glitchtip"}), 3600),
    "weekly_scorecard": ("info", frozenset({"email"}), 604800),
}


async def test_severity_rule_has_nine_seeded_rows(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT rule_name, severity, sinks, throttle_seconds, enabled "
                    "FROM audit.severity_rule"
                )
            )
        ).all()
    by_name = {r.rule_name: r for r in rows}
    missing = set(_EXPECTED) - set(by_name)
    assert not missing, f"missing severity_rule rows: {sorted(missing)}"
    for rule_name, (severity, sinks, throttle) in _EXPECTED.items():
        row = by_name[rule_name]
        assert row.severity == severity, f"{rule_name} severity mismatch"
        assert frozenset(row.sinks) == sinks, f"{rule_name} sinks mismatch: got {row.sinks}"
        assert int(row.throttle_seconds) == throttle, f"{rule_name} throttle mismatch"
        assert row.enabled is True, f"{rule_name} should be enabled by default"


async def test_severity_rule_seed_idempotent(engine: AsyncEngine) -> None:
    """Migration uses ON CONFLICT (rule_name) DO NOTHING; running the
    seed INSERT a second time must not duplicate or alter rows."""
    async with engine.begin() as conn:
        before = (
            await conn.execute(text("SELECT count(*)::int AS n FROM audit.severity_rule"))
        ).scalar_one()
    async with engine.begin() as conn:
        # Replay one row's INSERT shape; the ON CONFLICT clause should
        # make this a no-op even though the seeded row exists.
        await conn.execute(
            text(
                "INSERT INTO audit.severity_rule("
                "  rule_name, predicate, severity, sinks, throttle_seconds, enabled"
                ") VALUES ("
                "  'recency_sla_breach', 'replay', 'error', "
                "  CAST(ARRAY['glitchtip','slack'] AS TEXT[]), 300, true"
                ") ON CONFLICT (rule_name) DO NOTHING"
            )
        )
    async with engine.connect() as conn:
        after = (
            await conn.execute(text("SELECT count(*)::int AS n FROM audit.severity_rule"))
        ).scalar_one()
        replayed_predicate = (
            await conn.execute(
                text(
                    "SELECT predicate FROM audit.severity_rule "
                    "WHERE rule_name = 'recency_sla_breach'"
                )
            )
        ).scalar_one()
    assert after == before
    # The replayed predicate text was 'replay' but ON CONFLICT skipped
    # the UPDATE, so the original seed text remains.
    assert replayed_predicate != "replay"
