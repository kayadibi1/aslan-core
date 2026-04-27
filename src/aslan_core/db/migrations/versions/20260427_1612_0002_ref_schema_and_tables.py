"""ref schema and tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-27 16:12:26.968667

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS ref")

    op.execute("""
        CREATE TABLE ref.entity (
            entity_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            entity_type      TEXT NOT NULL,
            legal_name       TEXT NOT NULL,
            short_name       TEXT,
            country_code     CHAR(2) NOT NULL DEFAULT 'TR',
            domicile         TEXT,
            incorporation_dt DATE,
            fiscal_year_end  DATE,
            status           TEXT NOT NULL DEFAULT 'active',
            parent_entity_id UUID REFERENCES ref.entity(entity_id),
            metadata         JSONB NOT NULL DEFAULT '{}',
            source_id        TEXT NOT NULL,
            ingestion_run_id BIGINT NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT entity_type_check CHECK (entity_type IN
                ('company','fund','instrument','index','sovereign','sector','founder','other')),
            CONSTRAINT entity_status_check CHECK (status IN
                ('active','suspended','delisted','merged','dissolved'))
        );
    """)
    op.execute(
        "CREATE INDEX entity_legal_name_trgm ON ref.entity USING gin (legal_name gin_trgm_ops)"
    )
    op.execute("CREATE INDEX entity_parent ON ref.entity(parent_entity_id)")
    op.execute("CREATE INDEX entity_type_status ON ref.entity(entity_type, status)")

    op.execute("""
        CREATE TABLE ref.identifier (
            identifier_id    BIGSERIAL PRIMARY KEY,
            entity_id        UUID NOT NULL REFERENCES ref.entity(entity_id) ON DELETE RESTRICT,
            namespace        TEXT NOT NULL,
            value            TEXT NOT NULL,
            valid_from       DATE NOT NULL DEFAULT '1900-01-01',
            valid_to         DATE NOT NULL DEFAULT '9999-12-31',
            is_primary       BOOLEAN NOT NULL DEFAULT false,
            source_id        TEXT NOT NULL,
            ingestion_run_id BIGINT NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            EXCLUDE USING gist (
                namespace WITH =,
                value WITH =,
                daterange(valid_from, valid_to, '[)') WITH &&
            )
        );
    """)
    op.execute("CREATE INDEX identifier_lookup ON ref.identifier(namespace, value)")
    op.execute("CREATE INDEX identifier_entity ON ref.identifier(entity_id)")

    op.execute("""
        CREATE TABLE ref.entity_relationship (
            relationship_id  BIGSERIAL PRIMARY KEY,
            parent_id        UUID NOT NULL REFERENCES ref.entity(entity_id),
            child_id         UUID NOT NULL REFERENCES ref.entity(entity_id),
            rel_type         TEXT NOT NULL,
            weight           NUMERIC,
            valid_from       DATE NOT NULL DEFAULT '1900-01-01',
            valid_to         DATE NOT NULL DEFAULT '9999-12-31',
            metadata         JSONB NOT NULL DEFAULT '{}',
            source_id        TEXT NOT NULL,
            ingestion_run_id BIGINT NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (parent_id <> child_id),
            UNIQUE (parent_id, child_id, rel_type, valid_from)
        );
    """)
    op.execute("CREATE INDEX rel_parent ON ref.entity_relationship(parent_id, rel_type)")
    op.execute("CREATE INDEX rel_child ON ref.entity_relationship(child_id, rel_type)")

    op.execute("""
        CREATE TABLE ref.sector (
            sector_id        TEXT PRIMARY KEY,
            taxonomy         TEXT NOT NULL,
            code             TEXT NOT NULL,
            name_tr          TEXT NOT NULL,
            name_en          TEXT,
            parent_sector_id TEXT REFERENCES ref.sector(sector_id),
            UNIQUE (taxonomy, code)
        );
    """)

    op.execute("""
        CREATE TABLE ref.entity_sector (
            entity_id  UUID NOT NULL REFERENCES ref.entity(entity_id),
            sector_id  TEXT NOT NULL REFERENCES ref.sector(sector_id),
            is_primary BOOLEAN NOT NULL DEFAULT false,
            valid_from DATE NOT NULL DEFAULT '1900-01-01',
            valid_to   DATE NOT NULL DEFAULT '9999-12-31',
            PRIMARY KEY (entity_id, sector_id, valid_from)
        );
    """)

    op.execute("""
        CREATE TABLE ref.calendar (
            calendar_id TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            timezone    TEXT NOT NULL DEFAULT 'Europe/Istanbul'
        );
    """)

    op.execute("""
        CREATE TABLE ref.calendar_day (
            calendar_id TEXT NOT NULL REFERENCES ref.calendar(calendar_id),
            dt          DATE NOT NULL,
            is_session  BOOLEAN NOT NULL,
            session_open  TIME,
            session_close TIME,
            note        TEXT,
            PRIMARY KEY (calendar_id, dt)
        );
    """)

    op.execute("""
        CREATE TABLE ref.currency (
            currency_code CHAR(3) PRIMARY KEY,
            name          TEXT NOT NULL,
            minor_unit    SMALLINT NOT NULL DEFAULT 2
        );
    """)


def downgrade() -> None:
    for stmt in [
        "DROP TABLE IF EXISTS ref.currency",
        "DROP TABLE IF EXISTS ref.calendar_day",
        "DROP TABLE IF EXISTS ref.calendar",
        "DROP TABLE IF EXISTS ref.entity_sector",
        "DROP TABLE IF EXISTS ref.sector",
        "DROP TABLE IF EXISTS ref.entity_relationship",
        "DROP TABLE IF EXISTS ref.identifier",
        "DROP TABLE IF EXISTS ref.entity",
        "DROP SCHEMA IF EXISTS ref",
    ]:
        op.execute(stmt)
