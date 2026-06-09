"""ref.entity bitemporal upgrade via SCD-4 pattern.

Revision ID: 0046
Revises: 0045
Create Date: 2026-05-09 07:03:00

Per ``docs/specs/bitemporal-research-api/SCOPE.md`` D10 (revised).

**Why SCD-4 instead of replacing ref.entity_pkey:** ``ref.entity_pkey``
is referenced by 14+ FK constraints across ``agg.*``, ``doc.*``,
``kap.*``, ``ref.*``, ``ts.*``. Replacing it would require coordinated
re-creation of every dependent FK with a new shape, which is a
multi-week coordination outside this build's scope.

The SCD-4 pattern preserves the FK contract:

- ``ref.entity`` stays as the **current-state** pointer. PK still
  ``entity_id``. All 14+ FKs unchanged.
- ``ref.entity_version`` is a new **bitemporal history** table. PK
  ``(entity_id, as_of)``. Append-only (BEFORE UPDATE trigger blocks
  mutation). Captures every change to ``ref.entity`` via an AFTER
  INSERT OR UPDATE OR DELETE trigger.
- ``ref.entity_at(p_as_of)`` is a PIT SQL function reading from
  ``ref.entity_version`` via DISTINCT ON, returning the latest
  version of each entity at or before ``p_as_of``.
- Backfill: every existing ``ref.entity`` row is seeded into
  ``ref.entity_version`` with ``as_of = created_at`` and
  ``event_kind = 'created'``.

This preserves Moat 2 (PIT correctness on entity facts) without
touching FK constraints. The trade-off vs the original SCOPE.md D10
plan: ``ref.entity`` itself is not append-only at the schema level
(an UPDATE on ``ref.entity`` is still allowed); the append-only
contract lives on ``ref.entity_version``. Functionally equivalent for
PIT queries.

``ref.entity`` continues to receive INSERT, UPDATE, DELETE through
existing application paths; the AFTER trigger writes the post-state
row into ``ref.entity_version`` automatically.

Reversibility: drops trigger, function, and table. ``ref.entity``
itself is not touched.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0046"
down_revision: str | Sequence[str] | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. ref.entity_version — bitemporal history mirror.
    op.execute("""
        CREATE TABLE ref.entity_version (
            entity_id              UUID NOT NULL,
            as_of                  TIMESTAMPTZ NOT NULL DEFAULT now(),
            event_kind             TEXT NOT NULL CHECK (event_kind IN
                                       ('created', 'updated', 'merged',
                                        'split', 'renamed', 'deleted')),
            -- Mirror of ref.entity columns (snapshot at this as_of).
            entity_type            TEXT NOT NULL,
            legal_name             TEXT NOT NULL,
            short_name             TEXT,
            country_code           CHARACTER(2) NOT NULL,
            domicile               TEXT,
            incorporation_dt       DATE,
            fiscal_year_end        DATE,
            status                 TEXT NOT NULL,
            parent_entity_id       UUID,
            metadata               JSONB NOT NULL,
            -- Bitemporal lineage extras (per SCOPE.md D10).
            merged_from_entity_ids JSONB,
            -- Provenance (matches ref.entity provenance columns).
            source_id              TEXT NOT NULL,
            ingestion_run_id       BIGINT NOT NULL,
            actor_id               TEXT,
            actor_kind             TEXT,
            client_ip              INET,
            user_agent             TEXT,
            request_id             UUID,
            captured_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (entity_id, as_of)
        )
    """)
    op.execute(
        "COMMENT ON TABLE ref.entity_version IS "
        "'Per SCOPE.md D10 (revised SCD-4): bitemporal history of ref.entity. "
        "Append-only; captured by AFTER trigger on ref.entity. PIT queries "
        "via ref.entity_at(p_as_of).'"
    )
    op.execute("""
        CREATE INDEX entity_version_entity_idx
            ON ref.entity_version (entity_id, as_of DESC)
    """)
    op.execute("""
        CREATE INDEX entity_version_event_kind_idx
            ON ref.entity_version (event_kind, as_of DESC)
    """)

    # 2. AFTER INSERT/UPDATE/DELETE trigger on ref.entity — captures
    #    changes into ref.entity_version. Trigger function uses
    #    statement-level NEW/OLD for INSERT/UPDATE/DELETE respectively.
    op.execute("""
        CREATE OR REPLACE FUNCTION ref.fn_capture_entity_version()
        RETURNS trigger AS $$
        DECLARE
            v_event_kind TEXT;
            v_row        ref.entity%ROWTYPE;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                v_event_kind := 'created';
                v_row := NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                -- Distinguish status='merged' from regular updates.
                IF NEW.status = 'merged' AND OLD.status IS DISTINCT FROM 'merged' THEN
                    v_event_kind := 'merged';
                ELSIF NEW.legal_name IS DISTINCT FROM OLD.legal_name THEN
                    v_event_kind := 'renamed';
                ELSE
                    v_event_kind := 'updated';
                END IF;
                v_row := NEW;
            ELSIF TG_OP = 'DELETE' THEN
                v_event_kind := 'deleted';
                v_row := OLD;
            END IF;

            INSERT INTO ref.entity_version (
                entity_id, as_of, event_kind,
                entity_type, legal_name, short_name, country_code,
                domicile, incorporation_dt, fiscal_year_end, status,
                parent_entity_id, metadata, merged_from_entity_ids,
                source_id, ingestion_run_id, actor_id, actor_kind,
                client_ip, user_agent, request_id
            ) VALUES (
                v_row.entity_id, now(), v_event_kind,
                v_row.entity_type, v_row.legal_name, v_row.short_name, v_row.country_code,
                v_row.domicile, v_row.incorporation_dt, v_row.fiscal_year_end, v_row.status,
                v_row.parent_entity_id, v_row.metadata,
                CASE WHEN v_row.metadata ? 'merged_from_entity_ids'
                     THEN v_row.metadata->'merged_from_entity_ids'
                     ELSE NULL END,
                v_row.source_id, v_row.ingestion_run_id, v_row.actor_id, v_row.actor_kind,
                v_row.client_ip, v_row.user_agent, v_row.request_id
            )
            ON CONFLICT (entity_id, as_of) DO NOTHING;
            -- ON CONFLICT guard: if two updates land in the same microsecond
            -- (clock tick collision), keep the first; subsequent versions
            -- catch up on next change.

            RETURN NULL;  -- AFTER trigger; return value ignored.
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        DROP TRIGGER IF EXISTS entity_capture_version ON ref.entity
    """)
    op.execute("""
        CREATE TRIGGER entity_capture_version
            AFTER INSERT OR UPDATE OR DELETE ON ref.entity
            FOR EACH ROW
            EXECUTE FUNCTION ref.fn_capture_entity_version()
    """)

    # 3. Append-only enforcement on ref.entity_version itself.
    op.execute("""
        CREATE TRIGGER entity_version_no_update
            BEFORE UPDATE ON ref.entity_version
            FOR EACH ROW
            EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic()
    """)

    # 4. Backfill: seed every existing ref.entity row into
    #    ref.entity_version with as_of=created_at, event_kind='created'.
    op.execute("""
        INSERT INTO ref.entity_version (
            entity_id, as_of, event_kind,
            entity_type, legal_name, short_name, country_code,
            domicile, incorporation_dt, fiscal_year_end, status,
            parent_entity_id, metadata,
            source_id, ingestion_run_id, actor_id, actor_kind,
            client_ip, user_agent, request_id
        )
        SELECT
            entity_id, created_at, 'created',
            entity_type, legal_name, short_name, country_code,
            domicile, incorporation_dt, fiscal_year_end, status,
            parent_entity_id, metadata,
            source_id, ingestion_run_id, actor_id, actor_kind,
            client_ip, user_agent, request_id
        FROM ref.entity
        ON CONFLICT (entity_id, as_of) DO NOTHING
    """)
    # Also seed an 'updated' version where created_at <> updated_at.
    op.execute("""
        INSERT INTO ref.entity_version (
            entity_id, as_of, event_kind,
            entity_type, legal_name, short_name, country_code,
            domicile, incorporation_dt, fiscal_year_end, status,
            parent_entity_id, metadata,
            source_id, ingestion_run_id, actor_id, actor_kind,
            client_ip, user_agent, request_id
        )
        SELECT
            entity_id, updated_at, 'updated',
            entity_type, legal_name, short_name, country_code,
            domicile, incorporation_dt, fiscal_year_end, status,
            parent_entity_id, metadata,
            source_id, ingestion_run_id, actor_id, actor_kind,
            client_ip, user_agent, request_id
        FROM ref.entity
        WHERE updated_at > created_at
        ON CONFLICT (entity_id, as_of) DO NOTHING
    """)

    # 5. PIT function: ref.entity_at(p_as_of).
    op.execute("""
        CREATE OR REPLACE FUNCTION ref.entity_at(p_as_of TIMESTAMPTZ)
        RETURNS TABLE (
            entity_id              UUID,
            as_of                  TIMESTAMPTZ,
            event_kind             TEXT,
            entity_type            TEXT,
            legal_name             TEXT,
            short_name             TEXT,
            country_code           CHARACTER(2),
            domicile               TEXT,
            incorporation_dt       DATE,
            fiscal_year_end        DATE,
            status                 TEXT,
            parent_entity_id       UUID,
            metadata               JSONB,
            merged_from_entity_ids JSONB
        )
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT DISTINCT ON (v.entity_id)
                v.entity_id, v.as_of, v.event_kind,
                v.entity_type, v.legal_name, v.short_name, v.country_code,
                v.domicile, v.incorporation_dt, v.fiscal_year_end, v.status,
                v.parent_entity_id, v.metadata, v.merged_from_entity_ids
            FROM ref.entity_version v
            WHERE v.as_of <= p_as_of
              AND v.event_kind <> 'deleted'
            ORDER BY v.entity_id, v.as_of DESC;
        $$;
    """)

    # 6. Registry row.
    op.execute("""
        INSERT INTO aslan_core.bitemporal_table_registry
            (schema_name, table_name, entity_columns, as_of_column,
             pit_function_name, api_path, exposed_in_api, notes)
        VALUES
            ('ref', 'entity_version', ARRAY['entity_id']::text[], 'as_of',
             'ref.entity_at', '/v1/research/entities', true,
             'SCD-4 bitemporal history of ref.entity. ref.entity itself stays as '
             'current-state pointer to preserve 14+ FK contracts. Updates to '
             'ref.entity automatically captured by AFTER trigger.')
        ON CONFLICT (schema_name, table_name) DO UPDATE
        SET entity_columns = EXCLUDED.entity_columns,
            pit_function_name = EXCLUDED.pit_function_name,
            api_path = EXCLUDED.api_path,
            exposed_in_api = EXCLUDED.exposed_in_api,
            notes = EXCLUDED.notes
    """)

    op.execute("GRANT SELECT ON ref.entity_version TO aslan_dashboard")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON ref.entity_version FROM aslan_dashboard")
    op.execute("""
        DELETE FROM aslan_core.bitemporal_table_registry
            WHERE schema_name='ref' AND table_name='entity_version'
    """)
    op.execute("DROP FUNCTION IF EXISTS ref.entity_at(TIMESTAMPTZ)")
    op.execute("DROP TRIGGER IF EXISTS entity_capture_version ON ref.entity")
    op.execute("DROP FUNCTION IF EXISTS ref.fn_capture_entity_version()")
    op.execute("DROP TABLE IF EXISTS ref.entity_version")
