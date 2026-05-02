"""agg.entity_latest_snapshot — pre-computed entity summary view.

Revision ID: 0027
Revises: 0026
Create Date: 2026-05-02 00:02:00

Materialized view refreshed nightly via ``aslan agg refresh-snapshot``.
The unique index enables REFRESH MATERIALIZED VIEW CONCURRENTLY (no
exclusive lock during refresh).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE MATERIALIZED VIEW agg.entity_latest_snapshot AS
        SELECT
            e.entity_id,
            e.legal_name,
            e.entity_type,
            (SELECT i.value FROM ref.identifier i
               WHERE i.entity_id = e.entity_id AND i.namespace = 'bist_ticker'
                 AND now()::date BETWEEN i.valid_from AND i.valid_to
               ORDER BY i.is_primary DESC LIMIT 1) AS bist_ticker,
            (SELECT max(f.published_at) FROM doc.filing f
               WHERE f.entity_id = e.entity_id) AS last_filing_at,
            (SELECT count(*) FROM doc.filing f WHERE f.entity_id = e.entity_id
               AND f.published_at > now() - INTERVAL '90 days') AS filings_90d
        FROM ref.entity e
        WHERE e.status = 'active'
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX entity_latest_snapshot_pk ON agg.entity_latest_snapshot(entity_id)"
    )
    op.execute("GRANT SELECT ON agg.entity_latest_snapshot TO aslan_dashboard")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON agg.entity_latest_snapshot FROM aslan_dashboard")
    op.execute("DROP MATERIALIZED VIEW agg.entity_latest_snapshot")
