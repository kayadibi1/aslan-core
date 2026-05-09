"""dq M-AU-07: scorecard_snapshot

Revision ID: 0062
Revises: 0061
Create Date: 2026-05-09 15:09:00

Spec §5.10 — weekly materialized scorecard rows. The Sunday 23:55 UTC
``audit-scorecard`` cron computes ten metrics aggregating from the
``audit.*`` tables for the trailing 7 days, writes one row per metric
into this table, then emits ``audit.event(event_type='scorecard_generated')``.
The dispatcher's ``weekly_scorecard`` severity rule (seeded in 0059)
picks up that event row and emails the rendered HTML body to the
``ASLAN_AUDIT_EMAIL_TO`` recipient list.

The PK is ``(week_start, metric_name)`` so re-running the cron for the
same week is idempotent — the CLI uses
``ON CONFLICT (week_start, metric_name) DO UPDATE`` to overwrite
``actual`` / ``status`` / ``notes`` while preserving the original
``recorded_at``.

Roles:

  * ``audit_writer`` — INSERT only (the cron runs as audit_writer).
  * ``audit_reader`` — SELECT.
  * ``aslan_dashboard`` — SELECT (the dashboard's /dq/scorecard page is
    read-only).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0062"
down_revision: str | Sequence[str] | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE audit.scorecard_snapshot (
            week_start    DATE NOT NULL,
            metric_name   TEXT NOT NULL,
            target        TEXT NOT NULL,
            actual        TEXT NOT NULL,
            status        TEXT NOT NULL
                CHECK (status IN ('pass','warn','fail')),
            notes         TEXT,
            recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (week_start, metric_name)
        )
    """)
    op.execute(
        "CREATE INDEX scorecard_snapshot_week "
        "ON audit.scorecard_snapshot(week_start DESC, metric_name)"
    )

    # ── audit_writer: INSERT (and UPDATE via ON CONFLICT for the
    # idempotent re-run path; UPSERT requires both INSERT and UPDATE
    # to satisfy the rewrite). ──
    op.execute("GRANT INSERT, UPDATE ON audit.scorecard_snapshot TO audit_writer")

    # ── audit_reader + aslan_dashboard: SELECT-only ──
    op.execute("GRANT SELECT ON audit.scorecard_snapshot TO audit_reader")
    op.execute("GRANT SELECT ON audit.scorecard_snapshot TO aslan_dashboard")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.scorecard_snapshot")
