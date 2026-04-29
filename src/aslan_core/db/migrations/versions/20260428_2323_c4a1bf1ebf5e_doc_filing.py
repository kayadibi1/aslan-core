"""doc filing

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-28 23:23:24.852532

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE doc.filing (
            filing_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id          TEXT NOT NULL REFERENCES src.source(source_id),
            source_filing_ref  TEXT NOT NULL,
            entity_id          UUID REFERENCES ref.entity(entity_id) ON DELETE RESTRICT,
            kind               TEXT NOT NULL,
            subkind            TEXT,
            title              TEXT NOT NULL,
            language           CHAR(2) NOT NULL DEFAULT 'tr',
            published_at       TIMESTAMPTZ NOT NULL,
            period_start       DATE,
            period_end         DATE,
            source_url         TEXT,
            is_amendment       BOOLEAN NOT NULL DEFAULT false,
            previous_filing_id UUID REFERENCES doc.filing(filing_id),
            primary_object_key TEXT NOT NULL,
            primary_mime       TEXT NOT NULL,
            primary_sha256     CHAR(64) NOT NULL,
            primary_bytes      BIGINT NOT NULL,
            extracted_text_key TEXT,
            has_xbrl           BOOLEAN NOT NULL DEFAULT false,
            xbrl_object_key    TEXT,
            metadata           JSONB NOT NULL DEFAULT '{}',
            ingestion_run_id   BIGINT NOT NULL REFERENCES src.ingestion_run(ingestion_run_id),
            discovered_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            revision_no        INT NOT NULL,
            fts                tsvector GENERATED ALWAYS AS (
                setweight(to_tsvector('simple', coalesce(title,'')), 'A')
            ) STORED,
            -- Hash dedup
            UNIQUE (source_id, primary_sha256),
            -- Single-head invariant (codex 2026-04-28 fix)
            UNIQUE (source_id, source_filing_ref, revision_no)
        )
    """)
    op.execute("CREATE INDEX filing_entity_pub ON doc.filing(entity_id, published_at DESC)")
    op.execute("CREATE INDEX filing_kind_pub ON doc.filing(kind, published_at DESC)")
    op.execute("CREATE INDEX filing_fts ON doc.filing USING gin(fts)")
    op.execute("CREATE INDEX filing_published ON doc.filing(published_at DESC)")
    op.execute(
        "CREATE INDEX filing_source_ref_latest "
        "ON doc.filing(source_id, source_filing_ref, revision_no DESC)"
    )
    op.execute(
        "CREATE INDEX filing_amendment_chain ON doc.filing(previous_filing_id) "
        "WHERE previous_filing_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS doc.filing CASCADE")
