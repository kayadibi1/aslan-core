"""dq M-AU-06: regression_flag

Revision ID: 0061
Revises: 0060
Create Date: 2026-05-09 15:08:00

Spec §5.7 + §7.4 — automated period-over-period regression flags.
The nightly ``audit-regression-detect`` cron writes one row per
(entity, metric) where ``|shift_pct| > threshold_pct``; reviewer
(sidar) flips ``status`` from ``open`` to ``reviewed`` /
``dismissed`` / ``confirmed_bug`` via ``/dq/validation``.

Append-only at the row level. ``status`` is the single mutable
column (per spec §11 — kept on the original row to make
``WHERE status='open'`` index-friendly). Status mutations also
write ``audit.event(event_type='regression_flag_reviewed')`` for the
audit trail. v2 (NG5) auto-dismissal mutates the same column with
``review_note='auto-dismissed: justified by KAP filing <id>'`` and
emits ``audit.event(event_type='regression_auto_dismissed')``.

Roles:

  * ``audit_writer`` — INSERT only. The detector cron runs as
    audit_writer; auto-dismissal v2 is a status flip and uses
    audit_admin instead (it's a review action, not a write action).
  * ``audit_admin`` — UPDATE on ``status`` / ``reviewer`` /
    ``reviewed_at`` / ``review_note``. The dashboard's review POST
    runs under this role; the v2 auto-dismissal step in the
    regression-detect cron also runs under audit_admin since it's
    semantically a review action.
  * ``audit_reader`` — SELECT.
  * ``aslan_dashboard`` — SELECT (the dashboard process is read-only;
    the review POST goes through a separate write-capable role per
    the spot-check pattern).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0061"
down_revision: str | Sequence[str] | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE audit.regression_flag (
            flag_id         BIGSERIAL PRIMARY KEY,
            source          TEXT NOT NULL,
            record_table    TEXT NOT NULL,
            record_pk       JSONB NOT NULL,
            metric          TEXT NOT NULL,
            prior_value     NUMERIC,
            current_value   NUMERIC,
            shift_pct       NUMERIC NOT NULL,
            threshold_pct   NUMERIC NOT NULL,
            detected_at     TIMESTAMPTZ NOT NULL,
            status          TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','reviewed','dismissed','confirmed_bug')),
            reviewer        TEXT,
            reviewed_at     TIMESTAMPTZ,
            review_note     TEXT,
            recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX rf_open "
        "ON audit.regression_flag(source, detected_at DESC) "
        "WHERE status = 'open'"
    )
    op.execute("CREATE INDEX rf_metric_detected ON audit.regression_flag(metric, detected_at DESC)")

    # ── audit_writer: INSERT only (the detector cron) ──
    op.execute("GRANT INSERT ON audit.regression_flag TO audit_writer")
    op.execute("GRANT USAGE ON SEQUENCE audit.regression_flag_flag_id_seq TO audit_writer")

    # ── audit_admin: UPDATE on status/reviewer/reviewed_at/review_note ──
    # audit_admin already holds GRANT ALL via 0053; this column-level
    # GRANT is defence-in-depth in case a future migration narrows the
    # admin role. The dashboard review POST + v2 auto-dismissal run
    # under audit_admin.
    op.execute(
        "GRANT UPDATE (status, reviewer, reviewed_at, review_note) "
        "ON audit.regression_flag TO audit_admin"
    )

    # ── audit_reader + aslan_dashboard: SELECT-only ──
    op.execute("GRANT SELECT ON audit.regression_flag TO audit_reader")
    op.execute("GRANT SELECT ON audit.regression_flag TO aslan_dashboard")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.regression_flag")
