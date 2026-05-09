"""dq M-AU-02 part 1: recency_sla, recency_observation, evds_release_calendar

Revision ID: 0054
Revises: 0053
Create Date: 2026-05-09 15:01:00

Spec §6.0 + §5.2 + §5.11. Includes the seed for `audit.recency_sla`
per the §6.0 table; the EVDS calendar JSON seed lands in
migration 0055 with `audit.coverage_snapshot`.

`audit.recency_observation` is converted to a Timescale hypertable
on `observed_at` (chunk_time_interval = 1 day) so chunk-based
retention/compression can apply. The PK is composite
(observation_id, observed_at) per Timescale's requirement that the
partition column be part of any unique constraint.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0054"
down_revision: str | Sequence[str] | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE audit.recency_sla (
            source        TEXT NOT NULL,
            dimension     TEXT NOT NULL,
            sla_seconds   INT NOT NULL,
            alert_at_2x   BOOLEAN NOT NULL DEFAULT true,
            notes         TEXT,
            recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (source, dimension)
        )
    """)

    # Composite PK (observation_id, observed_at) is required because
    # Timescale's create_hypertable() needs the partition column to be
    # part of any unique/primary key constraint.
    op.execute("""
        CREATE TABLE audit.recency_observation (
            observation_id     BIGSERIAL,
            source             TEXT NOT NULL,
            observed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            upstream_latest_at TIMESTAMPTZ NOT NULL,
            db_latest_at       TIMESTAMPTZ NOT NULL,
            lag_seconds        INT GENERATED ALWAYS AS (
                EXTRACT(EPOCH FROM (upstream_latest_at - db_latest_at))::INT
            ) STORED,
            sla_target_seconds INT NOT NULL,
            sla_breached       BOOLEAN GENERATED ALWAYS AS (
                EXTRACT(EPOCH FROM (upstream_latest_at - db_latest_at))::INT
                    > sla_target_seconds
            ) STORED,
            probe_detail       JSONB,
            recorded_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (observation_id, observed_at)
        )
    """)
    op.execute(
        "SELECT create_hypertable('audit.recency_observation', 'observed_at', "
        "chunk_time_interval => INTERVAL '1 day')"
    )
    op.execute(
        "CREATE INDEX recency_src_obs ON audit.recency_observation(source, observed_at DESC)"
    )
    op.execute(
        "CREATE INDEX recency_breach ON audit.recency_observation"
        "(source, observed_at DESC) WHERE sla_breached = true"
    )

    op.execute("""
        CREATE TABLE audit.evds_release_calendar (
            series_code      TEXT NOT NULL,
            expected_at      TIMESTAMPTZ NOT NULL,
            grace_seconds    INT NOT NULL DEFAULT 14400,
            notes            TEXT,
            recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (series_code, expected_at)
        )
    """)

    # Seed audit.recency_sla per spec §6.0.
    op.execute("""
        INSERT INTO audit.recency_sla(source, dimension, sla_seconds, notes) VALUES
            ('kap',   'publish_to_db',
             300,
             'Existing tail-mode target; workspace CLAUDE.md §4 KAP latency claim'),
            ('kap',   'publish_to_db_high_priority',
             60,
             'Hot-path target once LISTEN/NOTIFY is live'),
            ('evds',  'release_window',
             14400,
             'Per audit.evds_release_calendar.grace_seconds default'),
            ('bist',  'trade_close_to_ohlcv',
             3600,
             'Skips weekends + TR holidays via ref.calendar_tr'),
            ('tefas', 'per_fund_cadence',
             172800,
             'Cadence learned per fund as rolling 90-d median update interval'),
            ('mkk',   'event_at_to_db',
             86400,
             'Conservative until baseline observed')
    """)

    op.execute("GRANT INSERT ON audit.recency_observation TO audit_writer")
    op.execute(
        "GRANT USAGE ON SEQUENCE audit.recency_observation_observation_id_seq TO audit_writer"
    )
    op.execute(
        "GRANT SELECT ON audit.recency_sla, audit.recency_observation, "
        "audit.evds_release_calendar TO audit_reader"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.evds_release_calendar")
    op.execute("DROP TABLE IF EXISTS audit.recency_observation")
    op.execute("DROP TABLE IF EXISTS audit.recency_sla")
