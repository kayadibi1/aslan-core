"""src and agg schemas

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-27 16:13:12.513336

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS src")
    op.execute("CREATE SCHEMA IF NOT EXISTS agg")  # empty per v0.1 scope

    op.execute("""
        CREATE TABLE src.source (
            source_id      TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            kind           TEXT NOT NULL,
            base_url       TEXT,
            license_status TEXT NOT NULL,
            metadata       JSONB NOT NULL DEFAULT '{}'
        );
    """)

    op.execute("""
        CREATE TABLE src.ingestion_run (
            ingestion_run_id BIGSERIAL PRIMARY KEY,
            source_id        TEXT NOT NULL REFERENCES src.source(source_id),
            job_name         TEXT NOT NULL,
            started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at      TIMESTAMPTZ,
            status           TEXT NOT NULL DEFAULT 'running',
            error_count      BIGINT NOT NULL DEFAULT 0,
            rows_written     BIGINT NOT NULL DEFAULT 0,
            docs_written     BIGINT NOT NULL DEFAULT 0,
            bytes_written    BIGINT NOT NULL DEFAULT 0,
            error            TEXT,
            config_hash      TEXT,
            metadata         JSONB NOT NULL DEFAULT '{}'
        );
    """)
    op.execute("CREATE INDEX run_source_started ON src.ingestion_run(source_id, started_at DESC)")

    op.execute("""
        CREATE TABLE src.watermark (
            source_id    TEXT NOT NULL REFERENCES src.source(source_id),
            job_name     TEXT NOT NULL,
            key          TEXT NOT NULL,
            cursor_value TEXT NOT NULL,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (source_id, job_name, key)
        );
    """)


def downgrade() -> None:
    for stmt in [
        "DROP TABLE IF EXISTS src.watermark",
        "DROP TABLE IF EXISTS src.ingestion_run",
        "DROP TABLE IF EXISTS src.source",
        "DROP SCHEMA IF EXISTS src",
        "DROP SCHEMA IF EXISTS agg",
    ]:
        op.execute(stmt)
