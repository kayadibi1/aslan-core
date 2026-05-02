"""agg.restatement_config — hyperinflation restatement job configuration.

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-02 00:01:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agg.restatement_config (
            config_id       BIGSERIAL PRIMARY KEY,
            name            TEXT NOT NULL UNIQUE,
            cpi_series_code TEXT NOT NULL,
            base_date       DATE NOT NULL,
            applies_from    DATE NOT NULL,
            applies_to      DATE NOT NULL DEFAULT '9999-12-31',
            method          TEXT NOT NULL,
            description     TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            actor_id        TEXT,
            actor_kind      TEXT
        )
        """
    )
    op.execute("GRANT SELECT ON agg.restatement_config TO aslan_dashboard")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON agg.restatement_config FROM aslan_dashboard")
    op.execute("DROP TABLE agg.restatement_config")
