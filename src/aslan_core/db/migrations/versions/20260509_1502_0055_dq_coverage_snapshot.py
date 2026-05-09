"""dq M-AU-02 part 2: coverage_snapshot + EVDS calendar JSON seed

Revision ID: 0055
Revises: 0054
Create Date: 2026-05-09 15:02:00

Spec §5.3 + §8 (coverage). Loads the EVDS release calendar JSON seed
from src/aslan_core/dq/data/evds_release_calendar.json so it ships
with the package — the calendar is small (single-digit-KB) and rarely
changes, so embedding it in the migration is simpler than running a
separate seed CLI on first boot.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from importlib import resources

from alembic import op
from sqlalchemy import text

revision: str = "0055"
down_revision: str | Sequence[str] | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE audit.coverage_snapshot (
            snapshot_id     BIGSERIAL PRIMARY KEY,
            source          TEXT NOT NULL,
            dimension       TEXT NOT NULL,
            observed_at     TIMESTAMPTZ NOT NULL,
            expected_count  INT,
            actual_count    INT,
            missing_ids     JSONB,
            coverage_pct    NUMERIC GENERATED ALWAYS AS (
                CASE WHEN expected_count > 0
                     THEN ROUND(100.0 * actual_count / expected_count, 2)
                     ELSE NULL END
            ) STORED,
            target_pct      NUMERIC NOT NULL DEFAULT 100.00,
            recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (source, dimension, observed_at)
        )
    """)
    op.execute(
        "CREATE INDEX coverage_src_dim_obs ON audit.coverage_snapshot"
        "(source, dimension, observed_at DESC)"
    )

    op.execute("GRANT INSERT ON audit.coverage_snapshot TO audit_writer")
    op.execute("GRANT USAGE ON SEQUENCE audit.coverage_snapshot_snapshot_id_seq TO audit_writer")
    op.execute("GRANT SELECT ON audit.coverage_snapshot TO audit_reader")

    # Seed audit.evds_release_calendar from JSON shipped with the package.
    raw = resources.files("aslan_core.dq.data").joinpath("evds_release_calendar.json").read_text()
    rows = json.loads(raw)
    if rows:
        bind = op.get_bind()
        for row in rows:
            # asyncpg requires a tz-aware datetime for TIMESTAMPTZ; the JSON
            # carries ISO-8601 with a trailing 'Z'. fromisoformat() in 3.12
            # handles 'Z' natively.
            bind.execute(
                text(
                    "INSERT INTO audit.evds_release_calendar"
                    "(series_code, expected_at, notes) VALUES"
                    "(:series_code, :expected_at, :notes) "
                    "ON CONFLICT DO NOTHING"
                ),
                {
                    "series_code": row["series_code"],
                    "expected_at": datetime.fromisoformat(row["expected_at"]),
                    "notes": row.get("notes"),
                },
            )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.coverage_snapshot")
