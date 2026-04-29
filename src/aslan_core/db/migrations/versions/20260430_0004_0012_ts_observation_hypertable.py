"""ts.observation Timescale hypertable

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-30 00:04:00

The fact table for v0.4.0. Composite PK ``(series_id, ts, as_of)`` is
the immutable identity of an observation — same key + identical
payload-hash = idempotent (codex F2); same key + different payload-hash
raises ObservationConflict.

  - ``value`` is DOUBLE PRECISION (codex F10) — IEEE-754 binary64 with
    no NUMERIC coercion. asyncpg's binary64 wire format preserves bits
    in both directions, which the v0.4 round-trip-byte-equality
    contract requires.
  - FK to ``ts.series_catalog`` is ON DELETE RESTRICT (NOT CASCADE) —
    a stray DELETE on the catalog must not silently destroy
    observation history.
  - Hypertable on ``ts`` (not ``as_of``) with chunk_time_interval=90d.
    90 days matches the cardinality of typical KAP / EVDS feeds; the
    audit-events 7-day chunks are too small for the observation
    write rate.
  - Two indexes:
        observation_series_ts_as_of  (series_id, ts, as_of DESC)
            for PIT replay queries (`DISTINCT ON ... ORDER BY ts ASC,
            as_of DESC`).
        observation_run              (ingestion_run_id)
            for forensic by-run lookups.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ts.observation (
            series_id              BIGINT NOT NULL
                REFERENCES ts.series_catalog(series_id) ON DELETE RESTRICT,
            ts                     TIMESTAMPTZ NOT NULL,
            as_of                  TIMESTAMPTZ NOT NULL,
            value                  DOUBLE PRECISION,
            value_text             TEXT,
            quality_flag           SMALLINT NOT NULL DEFAULT 0,
            ingestion_run_id       BIGINT NOT NULL
                REFERENCES src.ingestion_run(ingestion_run_id),
            payload_hash           CHAR(64) NOT NULL,
            metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
            -- audit columns (v0.3 contract)
            actor_id               TEXT,
            actor_kind             TEXT CHECK (actor_kind IN ('user','service','system')),
            client_ip              INET,
            user_agent             TEXT,
            request_id             UUID,
            PRIMARY KEY (series_id, ts, as_of),
            CHECK ((value IS NOT NULL) OR (value_text IS NOT NULL))
        )
    """)
    op.execute(
        "SELECT create_hypertable('ts.observation', 'ts', "
        "chunk_time_interval => INTERVAL '90 days')"
    )
    op.execute(
        "CREATE INDEX observation_series_ts_as_of ON ts.observation(series_id, ts, as_of DESC)"
    )
    op.execute("CREATE INDEX observation_run ON ts.observation(ingestion_run_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ts.observation")
