"""dq M-AU-05: spot_check_sample + spot_check_result

Revision ID: 0058
Revises: 0057
Create Date: 2026-05-09 15:05:00

Spec §5.5 + §7.2 — weekly random-draw + per-field labelled-truth
spot-check workflow. Both tables are append-only (the labeller
flow flips ``spot_check_sample.labelled`` from false→true once,
plus stamps ``labelled_at`` and ``labeller``; that single
``UPDATE`` is the only mutation surface, hence the narrow
column-level GRANT below).

For KAP samples the labelled result is ALSO mirrored into
``agg.filing_event_label`` (M3 deliverable) so spot-checks
contribute to the labelled validation corpus rather than
duplicating it. The mirror happens in the Python module via
``INSERT … ON CONFLICT DO NOTHING``; this migration carries a
soft-link ``label_event_id BIGINT`` on ``spot_check_result``
(no hard FK — ``agg.filing_event_label`` may not be present on
every branch / environment).

Roles:
  * ``audit_writer`` — INSERT on both tables; UPDATE on the three
    labelled-flow columns of ``spot_check_sample``.
  * ``audit_reader`` — SELECT on both.
  * ``aslan_dashboard`` — SELECT on both. Write happens via the
    labeller's session under a different role; the dashboard
    process is read-only by design (migration 0020).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0058"
down_revision: str | Sequence[str] | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE audit.spot_check_sample (
            sample_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source         TEXT NOT NULL,
            drawn_at       TIMESTAMPTZ NOT NULL,
            record_table   TEXT NOT NULL,
            record_pk      JSONB NOT NULL,
            stratum        TEXT,
            labelled       BOOLEAN NOT NULL DEFAULT false,
            labelled_at    TIMESTAMPTZ,
            labeller       TEXT,
            recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX scs_pending "
        "ON audit.spot_check_sample(source, drawn_at DESC) "
        "WHERE labelled = false"
    )

    op.execute("""
        CREATE TABLE audit.spot_check_result (
            result_id      BIGSERIAL PRIMARY KEY,
            sample_id      UUID NOT NULL REFERENCES audit.spot_check_sample,
            field          TEXT NOT NULL,
            db_value       TEXT,
            truth_value    TEXT,
            variance_pct   NUMERIC,
            matches        BOOLEAN NOT NULL,
            label_note     TEXT,
            labeller       TEXT NOT NULL,
            label_event_id BIGINT,
            recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX scr_sample ON audit.spot_check_result(sample_id)")
    op.execute(
        "CREATE INDEX scr_match "
        "ON audit.spot_check_result(matches, recorded_at DESC)"
    )

    # Writer role: full INSERT on sample + result; UPDATE only on the
    # three columns the labelling flow flips on first label.
    op.execute("GRANT INSERT ON audit.spot_check_sample TO audit_writer")
    op.execute(
        "GRANT UPDATE (labelled, labelled_at, labeller) "
        "ON audit.spot_check_sample TO audit_writer"
    )
    op.execute("GRANT INSERT ON audit.spot_check_result TO audit_writer")
    op.execute(
        "GRANT USAGE ON SEQUENCE audit.spot_check_result_result_id_seq TO audit_writer"
    )

    # Reader role: SELECT on both.
    op.execute("GRANT SELECT ON audit.spot_check_sample TO audit_reader")
    op.execute("GRANT SELECT ON audit.spot_check_result TO audit_reader")

    # Dashboard role: SELECT only. v1 write-flow assumes the labeller
    # authenticates against a write-capable role (audit_writer); the
    # dashboard process itself remains read-only.
    op.execute("GRANT SELECT ON audit.spot_check_sample TO aslan_dashboard")
    op.execute("GRANT SELECT ON audit.spot_check_result TO aslan_dashboard")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.spot_check_result")
    op.execute("DROP TABLE IF EXISTS audit.spot_check_sample")
