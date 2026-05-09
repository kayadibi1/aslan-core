"""dq M-AU-06: seed audit.severity_rule with the 9 spec §10.3 rules

Revision ID: 0059
Revises: 0058
Create Date: 2026-05-09 15:06:00

Spec §10.3 — alert routing config table. Migration 0053 created the
empty table; this migration seeds the nine canonical rules driving
the M3 dispatcher (`aslan_core.dq.alert_dispatch`):

    | rule_name                          | severity  | sinks                 | throttle |
    | ---------------------------------- | --------- | --------------------- | -------- |
    | recency_sla_breach                 | error     | glitchtip, slack      | 300      |
    | recency_sla_breach_2x              | critical  | glitchtip, slack      | 60       |
    | validation_pass_rate_below_95      | warn      | glitchtip             | 1800     |
    | validation_pass_rate_below_90      | error     | glitchtip, slack      | 600      |
    | coverage_below_target              | warn      | glitchtip, email      | 3600     |
    | coverage_below_90pct               | error     | glitchtip, slack      | 600      |
    | bloomberg_loses_field              | error     | glitchtip, email      | 86400    |
    | regression_flag_critical_entity    | warn      | glitchtip             | 3600     |
    | weekly_scorecard                   | info      | email                 | 604800   |

The ``predicate`` column is a SQL expression that evaluates against
recent ``audit.*`` rows. The dispatcher (`alert_dispatch.evaluate_and_enqueue`)
parses + executes each predicate over the last ``throttle_seconds``
window. See `aslan_core.dq.alert_dispatch._RULE_QUERIES` for the
canonical mapping from ``rule_name`` to the SELECT statement actually
executed; the ``predicate`` text is human-readable documentation that
must stay aligned with the dispatcher code.

Idempotent: each row uses ``INSERT … ON CONFLICT (rule_name) DO NOTHING``
so re-running the migration on a database that already has these rules
(e.g. seeded via a sibling environment script) is a no-op.

The migration intentionally writes through the audit-rule trigger
installed by 0053, which lands a ``severity_rule_changed`` row in
``audit.event`` for every INSERT. That trigger fires on
``current_user`` — during the migration run the user is the migration
role, not a human; the resulting audit rows attribute the seed to the
role, which is the correct provenance.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0059"
down_revision: str | Sequence[str] | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Each tuple: (rule_name, predicate, severity, sinks, throttle_seconds).
# The ``predicate`` text MUST stay aligned with the SELECT statement in
# ``aslan_core.dq.alert_dispatch._RULE_QUERIES`` — the dispatcher does
# NOT parse the text at runtime; this column is human-readable
# documentation pinned next to the seed for auditability.
_SEED_RULES: tuple[tuple[str, str, str, list[str], int], ...] = (
    (
        "recency_sla_breach",
        "new audit.recency_observation row with sla_breached = true",
        "error",
        ["glitchtip", "slack"],
        300,
    ),
    (
        "recency_sla_breach_2x",
        "audit.recency_observation row where lag_seconds > 2 * sla_target_seconds",
        "critical",
        ["glitchtip", "slack"],
        60,
    ),
    (
        "validation_pass_rate_below_95",
        "rolling-1h validation pass-rate per source < 95%",
        "warn",
        ["glitchtip"],
        1800,
    ),
    (
        "validation_pass_rate_below_90",
        "rolling-1h validation pass-rate per source < 90%",
        "error",
        ["glitchtip", "slack"],
        600,
    ),
    (
        "coverage_below_target",
        "new audit.coverage_snapshot row with coverage_pct < target_pct",
        "warn",
        ["glitchtip", "email"],
        3600,
    ),
    (
        "coverage_below_90pct",
        "audit.coverage_snapshot row with coverage_pct < 90",
        "error",
        ["glitchtip", "slack"],
        600,
    ),
    (
        "bloomberg_loses_field",
        "new audit.bloomberg_comparison_cell with aslan_advantage = 'loses'",
        "error",
        ["glitchtip", "email"],
        86400,
    ),
    (
        "regression_flag_critical_entity",
        "new audit.regression_flag for an entity in the curated top-50 list",
        "warn",
        ["glitchtip"],
        3600,
    ),
    (
        "weekly_scorecard",
        "weekly cron audit-scorecard run completes",
        "info",
        ["email"],
        604800,
    ),
)


_INSERT_RULE = text(
    "INSERT INTO audit.severity_rule("
    "  rule_name, predicate, severity, sinks, throttle_seconds, enabled"
    ") VALUES ("
    "  :rule_name, :predicate, :severity, CAST(:sinks AS TEXT[]), "
    "  :throttle_seconds, true"
    ") ON CONFLICT (rule_name) DO NOTHING"
)


def upgrade() -> None:
    bind = op.get_bind()
    for rule_name, predicate, severity, sinks, throttle_seconds in _SEED_RULES:
        bind.execute(
            _INSERT_RULE,
            {
                "rule_name": rule_name,
                "predicate": predicate,
                "severity": severity,
                "sinks": sinks,
                "throttle_seconds": throttle_seconds,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    rule_names = [r[0] for r in _SEED_RULES]
    bind.execute(
        text("DELETE FROM audit.severity_rule WHERE rule_name = ANY(:names)"),
        {"names": rule_names},
    )
