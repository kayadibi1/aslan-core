"""ts schema + ts.series_catalog reference table

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-30 00:03:00

The reference table for the v0.4.0 timeseries surface — rare writes,
every observation INSERT joins on it. Includes the v0.3 audit-column
contract (actor_id / actor_kind / client_ip / user_agent / request_id),
``pii_class`` with default ``'none'`` (codex F4), free-form ``metadata``
JSONB defaulting to ``'{}'``, and three indexes:

  - series_catalog_entity         partial index on entity_id
  - series_catalog_source_metric  composite index on (source_id, metric)
  - series_catalog_pii            partial index on rows with PII

Schema namespace ``ts`` is created here and reused by 0012 / 0013.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS ts")
    op.execute("""
        CREATE TABLE ts.series_catalog (
            series_id              BIGSERIAL PRIMARY KEY,
            series_code            TEXT NOT NULL UNIQUE,
            source_id              TEXT NOT NULL REFERENCES src.source(source_id),
            entity_id              UUID REFERENCES ref.entity(entity_id),
            metric                 TEXT NOT NULL,
            frequency              TEXT NOT NULL CHECK (
                frequency IN ('tick','1s','1m','5m','15m','30m',
                              '1h','1d','1w','1mo','1q','1y','irregular')
            ),
            unit                   TEXT NOT NULL,
            currency_code          CHAR(3) REFERENCES ref.currency(currency_code),
            restatement_basis      TEXT NOT NULL DEFAULT 'nominal'
                CHECK (restatement_basis IN ('nominal','as_reported','restated','adjusted')),
            accounting_standard    TEXT,
            consolidation          TEXT,
            period_type            TEXT,
            description            TEXT,
            pii_class              TEXT NOT NULL DEFAULT 'none'
                CHECK (pii_class IN ('none','pseudonymous','identifying')),
            metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
            -- audit columns (v0.3 contract)
            actor_id               TEXT,
            actor_kind             TEXT CHECK (actor_kind IN ('user','service','system')),
            client_ip              INET,
            user_agent             TEXT,
            request_id             UUID,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX series_catalog_entity ON ts.series_catalog(entity_id) "
        "WHERE entity_id IS NOT NULL"
    )
    op.execute("CREATE INDEX series_catalog_source_metric ON ts.series_catalog(source_id, metric)")
    op.execute(
        "CREATE INDEX series_catalog_pii ON ts.series_catalog(pii_class) WHERE pii_class != 'none'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ts.series_catalog")
    # Do NOT drop the schema — 0012 / 0013 also live in it.
