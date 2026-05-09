"""dq M-AU-03: validation_failure

Revision ID: 0056
Revises: 0055
Create Date: 2026-05-09 15:03:00

Spec §5.4 + §7.1 (in-line schema validation) + §7.3 (cross-source
consistency). Both call paths INSERT into the same table; the
`rule_name` column distinguishes them.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0056"
down_revision: str | Sequence[str] | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE audit.validation_failure (
            failure_id    BIGSERIAL PRIMARY KEY,
            source        TEXT NOT NULL,
            rule_name     TEXT NOT NULL,
            severity      TEXT NOT NULL
                CHECK (severity IN ('info','warn','error','critical')),
            record_table  TEXT NOT NULL,
            record_pk     JSONB NOT NULL,
            detected_at   TIMESTAMPTZ NOT NULL,
            detail        JSONB NOT NULL,
            recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX vf_rule_detected ON audit.validation_failure(rule_name, detected_at DESC)"
    )
    op.execute(
        "CREATE INDEX vf_src_sev_detected "
        "ON audit.validation_failure(source, severity, detected_at DESC)"
    )

    op.execute("GRANT INSERT ON audit.validation_failure TO audit_writer")
    op.execute("GRANT USAGE ON SEQUENCE audit.validation_failure_failure_id_seq TO audit_writer")
    op.execute("GRANT SELECT ON audit.validation_failure TO audit_reader")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.validation_failure")
