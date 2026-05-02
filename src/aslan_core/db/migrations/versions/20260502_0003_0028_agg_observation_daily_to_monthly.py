"""agg.observation_daily_to_monthly — TimescaleDB continuous aggregate.

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-02 00:03:00

Rolls daily observations into monthly rollups. No-op on an empty
ts.observation hypertable — activates automatically once data flows.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Continuous aggregates cannot be created inside a transaction block.
    conn = op.get_bind()
    conn.execute(text("COMMIT"))
    conn.execute(
        text(
            """
            CREATE MATERIALIZED VIEW agg.observation_daily_to_monthly
            WITH (timescaledb.continuous) AS
            SELECT
                series_id,
                time_bucket('1 month', ts) AS month,
                last(value, ts) AS month_end_value,
                avg(value) AS month_avg_value,
                min(value) AS month_min,
                max(value) AS month_max,
                count(*) AS n_obs
            FROM ts.observation
            WHERE value IS NOT NULL
            GROUP BY series_id, month
            """
        )
    )
    conn.execute(
        text(
            """
            SELECT add_continuous_aggregate_policy('agg.observation_daily_to_monthly',
                start_offset => INTERVAL '1 year',
                end_offset   => INTERVAL '1 day',
                schedule_interval => INTERVAL '1 hour')
            """
        )
    )
    conn.execute(text("GRANT SELECT ON agg.observation_daily_to_monthly TO aslan_dashboard"))
    conn.execute(text("BEGIN"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("COMMIT"))
    conn.execute(text("REVOKE SELECT ON agg.observation_daily_to_monthly FROM aslan_dashboard"))
    conn.execute(text("DROP MATERIALIZED VIEW agg.observation_daily_to_monthly"))
    conn.execute(text("BEGIN"))
