"""dq M-AU-04: dashboard SELECT grants for audit.* dq tables

Revision ID: 0057
Revises: 0056
Create Date: 2026-05-09 15:04:00

The dashboard process logs in as the `aslan_dashboard` LOGIN role
(migration 0020). M0's dq audit tables (`audit.recency_observation`,
`audit.coverage_snapshot`, `audit.validation_failure`,
`audit.recency_sla`, `audit.evds_release_calendar`, `audit.event`,
`audit.sync_log`) granted SELECT only to `audit_reader` — a NOLOGIN
operational role. Without this migration the dashboard's M1
/dq/* pages fail with InsufficientPrivilegeError when querying any
audit.* table.

This migration extends `aslan_dashboard` with table-level SELECT on
the seven dq audit tables. Operational role separation is preserved
(audit_writer / audit_reader / audit_admin still have their narrow
grants); we are *adding* dashboard SELECT, not relaxing the M0
boundary.

The dashboard role does NOT receive INSERT, UPDATE, or DELETE on
any audit.* table — those remain audit_writer-only. The dashboard is
read-only by design (default_transaction_read_only=on, set by
migration 0020).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0057"
down_revision: str | Sequence[str] | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DQ_AUDIT_TABLES: tuple[str, ...] = (
    "audit.sync_log",
    "audit.event",
    "audit.severity_rule",
    "audit.alert_dispatch",
    "audit.recency_sla",
    "audit.recency_observation",
    "audit.evds_release_calendar",
    "audit.coverage_snapshot",
    "audit.validation_failure",
)


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA audit TO aslan_dashboard")
    for tbl in _DQ_AUDIT_TABLES:
        op.execute(f"GRANT SELECT ON {tbl} TO aslan_dashboard")


def downgrade() -> None:
    for tbl in _DQ_AUDIT_TABLES:
        op.execute(f"REVOKE SELECT ON {tbl} FROM aslan_dashboard")  # noqa: S608
