"""kap.disclosures bitemporal upgrade via SCD-4 pattern (SCOPE.md D11, D3).

Revision ID: 0051
Revises: 0050
Create Date: 2026-05-09 07:08:00

Per ``docs/specs/bitemporal-research-api/SCOPE.md`` D11 and D3.

**Why SCD-4 instead of in-place restructuring:** ``kap.disclosures``
is referenced by 4 FK constraints (`disclosure_attachments`,
`disclosure_parse_state`, `financial_parse_state`,
`parsed_disclosures`) and is mutated continuously by `crawl`'s
body-fetcher service (UPDATE OF body_fetched). Replacing the table
or its PK would require coordinated patches in `crawl`. The SCD-4
pattern preserves the FK contract and the existing `crawl` write
path:

- ``kap.disclosures`` stays as the **current-state** pointer. PK
  still ``disclosure_id``. All 4 FKs unchanged. The `crawl`
  body-fetcher continues to UPDATE `body_fetched=true` and
  `body_fetched_at` on the same row — no `crawl` code change
  required.
- ``kap.disclosures_version`` is a new bitemporal history table.
  PK ``(disclosure_id, as_of)``. Append-only.
- An AFTER INSERT/UPDATE/DELETE trigger on ``kap.disclosures``
  captures every change into ``kap.disclosures_version`` with
  ``as_of = now()`` and ``event_kind`` discriminating the change
  type (`indexed`, `body_fetched`, `republished`, `deleted`).
- ``kap.disclosures_at(p_as_of)`` is a PIT SQL function reading from
  the version table.

**Pre-bitemporal backfill (D3):** the 462k existing rows in
``kap.disclosures`` are seeded into ``kap.disclosures_version`` as:
- one row per existing disclosure with ``as_of=index_fetched_at``
  and ``event_kind='indexed'``;
- a second row for the ~89.7k rows where ``body_fetched=true``,
  with ``as_of=body_fetched_at`` and ``event_kind='body_fetched'``;
- ``as_of_provenance`` recorded per row (``index_fetched_at`` /
  ``body_fetched_at`` / ``pre_bitemporal_unknown``).

Rows where the timestamps are NULL surface in the API only with
``?include_pre_bitemporal=true`` per D3.

**Note on the existing kap triggers:** ``trg_mirror_to_doc_filing``
and ``trg_notify_body_ready`` (AFTER UPDATE OF body_fetched on
``kap.disclosures``) remain in place; they fire alongside the new
``trg_capture_disclosure_version``.

Reversibility: drops the version table, the PIT function, the
trigger, and the trigger function. ``kap.disclosures`` is unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0051"
down_revision: str | Sequence[str] | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. kap.disclosures_version — bitemporal history mirror.
    op.execute("""
        CREATE TABLE kap.disclosures_version (
            disclosure_id          TEXT NOT NULL,
            as_of                  TIMESTAMPTZ NOT NULL DEFAULT now(),
            event_kind             TEXT NOT NULL CHECK (event_kind IN
                                       ('indexed', 'body_fetched',
                                        'republished', 'updated', 'deleted')),
            as_of_provenance       TEXT NOT NULL DEFAULT 'live'
                                   CHECK (as_of_provenance IN
                                       ('live', 'index_fetched_at',
                                        'body_fetched_at', 'published_at',
                                        'pre_bitemporal_unknown')),
            -- Snapshot of kap.disclosures columns at this as_of.
            entity_id              UUID NOT NULL,
            kap_id                 TEXT NOT NULL,
            published_at           TIMESTAMPTZ NOT NULL,
            category_code          TEXT NOT NULL,
            subcategory_code       TEXT,
            title                  TEXT NOT NULL,
            language               kap.disclosure_language NOT NULL,
            kap_url                TEXT NOT NULL,
            is_amendment           BOOLEAN NOT NULL DEFAULT false,
            parent_disclosure_id   TEXT,
            body_fetched           BOOLEAN NOT NULL DEFAULT false,
            index_fetched_at       TIMESTAMPTZ NOT NULL,
            raw_index_storage_key  TEXT NOT NULL,
            raw_body_storage_key   TEXT,
            body_fetched_at        TIMESTAMPTZ,
            raw_body_sha256        CHARACTER(64),
            raw_body_bytes         BIGINT,
            raw_body_mime          TEXT,
            captured_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (disclosure_id, as_of)
        )
    """)
    op.execute(
        "COMMENT ON TABLE kap.disclosures_version IS "
        "'Per SCOPE.md D11/D3 (revised SCD-4): bitemporal history of "
        "kap.disclosures. Append-only; AFTER trigger captures changes. "
        "PIT queries via kap.disclosures_at(p_as_of). Pre-bitemporal "
        "rows have as_of_provenance set to indicate proxy timestamp.'"
    )
    op.execute("""
        CREATE INDEX disclosures_version_disclosure_idx
            ON kap.disclosures_version (disclosure_id, as_of DESC)
    """)
    op.execute("""
        CREATE INDEX disclosures_version_event_kind_idx
            ON kap.disclosures_version (event_kind, as_of DESC)
    """)
    op.execute("""
        CREATE INDEX disclosures_version_entity_idx
            ON kap.disclosures_version (entity_id, as_of DESC)
    """)
    op.execute("""
        CREATE INDEX disclosures_version_provenance_idx
            ON kap.disclosures_version (as_of_provenance)
            WHERE as_of_provenance <> 'live'
    """)

    # 2. AFTER trigger function and trigger on kap.disclosures.
    op.execute("""
        CREATE OR REPLACE FUNCTION kap.fn_capture_disclosure_version()
        RETURNS trigger AS $$
        DECLARE
            v_event_kind TEXT;
            v_row        kap.disclosures%ROWTYPE;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                v_event_kind := 'indexed';
                v_row := NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                IF NEW.body_fetched IS DISTINCT FROM OLD.body_fetched
                   AND NEW.body_fetched = true THEN
                    v_event_kind := 'body_fetched';
                ELSIF NEW.parent_disclosure_id IS DISTINCT FROM OLD.parent_disclosure_id THEN
                    v_event_kind := 'republished';
                ELSE
                    v_event_kind := 'updated';
                END IF;
                v_row := NEW;
            ELSIF TG_OP = 'DELETE' THEN
                v_event_kind := 'deleted';
                v_row := OLD;
            END IF;

            INSERT INTO kap.disclosures_version (
                disclosure_id, as_of, event_kind, as_of_provenance,
                entity_id, kap_id, published_at, category_code,
                subcategory_code, title, language, kap_url, is_amendment,
                parent_disclosure_id, body_fetched, index_fetched_at,
                raw_index_storage_key, raw_body_storage_key,
                body_fetched_at, raw_body_sha256, raw_body_bytes, raw_body_mime
            ) VALUES (
                v_row.disclosure_id, now(), v_event_kind, 'live',
                v_row.entity_id, v_row.kap_id, v_row.published_at, v_row.category_code,
                v_row.subcategory_code, v_row.title, v_row.language, v_row.kap_url, v_row.is_amendment,
                v_row.parent_disclosure_id, v_row.body_fetched, v_row.index_fetched_at,
                v_row.raw_index_storage_key, v_row.raw_body_storage_key,
                v_row.body_fetched_at, v_row.raw_body_sha256, v_row.raw_body_bytes, v_row.raw_body_mime
            )
            ON CONFLICT (disclosure_id, as_of) DO NOTHING;

            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        DROP TRIGGER IF EXISTS trg_capture_disclosure_version ON kap.disclosures
    """)
    op.execute("""
        CREATE TRIGGER trg_capture_disclosure_version
            AFTER INSERT OR UPDATE OR DELETE ON kap.disclosures
            FOR EACH ROW
            EXECUTE FUNCTION kap.fn_capture_disclosure_version()
    """)

    # 3. Append-only enforcement on the version table.
    op.execute("""
        CREATE TRIGGER disclosures_version_no_update
            BEFORE UPDATE ON kap.disclosures_version
            FOR EACH ROW
            EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic()
    """)

    # 4. Backfill: seed the 462k pre-bitemporal rows into the version
    #    table. Two passes: 'indexed' from index_fetched_at, then
    #    'body_fetched' for rows where body_fetched=true.
    op.execute("""
        INSERT INTO kap.disclosures_version (
            disclosure_id, as_of, event_kind, as_of_provenance,
            entity_id, kap_id, published_at, category_code,
            subcategory_code, title, language, kap_url, is_amendment,
            parent_disclosure_id, body_fetched, index_fetched_at,
            raw_index_storage_key, raw_body_storage_key,
            body_fetched_at, raw_body_sha256, raw_body_bytes, raw_body_mime
        )
        SELECT
            disclosure_id, index_fetched_at, 'indexed',
            CASE WHEN index_fetched_at IS NOT NULL THEN 'index_fetched_at'
                 ELSE 'pre_bitemporal_unknown' END,
            entity_id, kap_id, published_at, category_code,
            subcategory_code, title, language, kap_url, is_amendment,
            parent_disclosure_id,
            -- Initial 'indexed' state always has body_fetched=false.
            false,
            index_fetched_at,
            raw_index_storage_key, NULL,
            NULL, NULL, NULL, NULL
        FROM kap.disclosures
        ON CONFLICT (disclosure_id, as_of) DO NOTHING
    """)
    op.execute("""
        INSERT INTO kap.disclosures_version (
            disclosure_id, as_of, event_kind, as_of_provenance,
            entity_id, kap_id, published_at, category_code,
            subcategory_code, title, language, kap_url, is_amendment,
            parent_disclosure_id, body_fetched, index_fetched_at,
            raw_index_storage_key, raw_body_storage_key,
            body_fetched_at, raw_body_sha256, raw_body_bytes, raw_body_mime
        )
        SELECT
            disclosure_id, body_fetched_at, 'body_fetched',
            'body_fetched_at',
            entity_id, kap_id, published_at, category_code,
            subcategory_code, title, language, kap_url, is_amendment,
            parent_disclosure_id, true, index_fetched_at,
            raw_index_storage_key, raw_body_storage_key,
            body_fetched_at, raw_body_sha256, raw_body_bytes, raw_body_mime
        FROM kap.disclosures
        WHERE body_fetched = true
          AND body_fetched_at IS NOT NULL
        ON CONFLICT (disclosure_id, as_of) DO NOTHING
    """)

    # 5. PIT function. Returns the disclosures-shaped row at the
    #    requested as_of, excluding rows last seen as 'deleted'.
    op.execute("""
        CREATE OR REPLACE FUNCTION kap.disclosures_at(p_as_of TIMESTAMPTZ)
        RETURNS TABLE (
            disclosure_id          TEXT,
            as_of                  TIMESTAMPTZ,
            event_kind             TEXT,
            as_of_provenance       TEXT,
            entity_id              UUID,
            kap_id                 TEXT,
            published_at           TIMESTAMPTZ,
            category_code          TEXT,
            subcategory_code       TEXT,
            title                  TEXT,
            language               kap.disclosure_language,
            kap_url                TEXT,
            is_amendment           BOOLEAN,
            parent_disclosure_id   TEXT,
            body_fetched           BOOLEAN,
            index_fetched_at       TIMESTAMPTZ,
            raw_index_storage_key  TEXT,
            raw_body_storage_key   TEXT,
            body_fetched_at        TIMESTAMPTZ,
            raw_body_sha256        CHARACTER(64),
            raw_body_bytes         BIGINT,
            raw_body_mime          TEXT
        )
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT DISTINCT ON (v.disclosure_id)
                v.disclosure_id, v.as_of, v.event_kind, v.as_of_provenance,
                v.entity_id, v.kap_id, v.published_at, v.category_code,
                v.subcategory_code, v.title, v.language, v.kap_url, v.is_amendment,
                v.parent_disclosure_id, v.body_fetched, v.index_fetched_at,
                v.raw_index_storage_key, v.raw_body_storage_key,
                v.body_fetched_at, v.raw_body_sha256, v.raw_body_bytes, v.raw_body_mime
            FROM kap.disclosures_version v
            WHERE v.as_of <= p_as_of
              AND v.event_kind <> 'deleted'
            ORDER BY v.disclosure_id, v.as_of DESC;
        $$;
    """)

    # 6. Registry row.
    op.execute("""
        INSERT INTO aslan_core.bitemporal_table_registry
            (schema_name, table_name, entity_columns, as_of_column,
             pit_function_name, api_path, exposed_in_api, notes)
        VALUES
            ('kap', 'disclosures_version', ARRAY['disclosure_id']::text[], 'as_of',
             'kap.disclosures_at', '/v1/research/disclosures', true,
             'SCD-4 bitemporal history of kap.disclosures. Backfilled from '
             'index_fetched_at + body_fetched_at; pre_bitemporal rows are '
             'tagged via as_of_provenance.')
        ON CONFLICT (schema_name, table_name) DO UPDATE
        SET entity_columns = EXCLUDED.entity_columns,
            pit_function_name = EXCLUDED.pit_function_name,
            api_path = EXCLUDED.api_path,
            exposed_in_api = EXCLUDED.exposed_in_api,
            notes = EXCLUDED.notes
    """)

    op.execute("GRANT SELECT ON kap.disclosures_version TO aslan_dashboard")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON kap.disclosures_version FROM aslan_dashboard")
    op.execute("""
        DELETE FROM aslan_core.bitemporal_table_registry
            WHERE schema_name='kap' AND table_name='disclosures_version'
    """)
    op.execute("DROP FUNCTION IF EXISTS kap.disclosures_at(TIMESTAMPTZ)")
    op.execute("DROP TRIGGER IF EXISTS trg_capture_disclosure_version ON kap.disclosures")
    op.execute("DROP FUNCTION IF EXISTS kap.fn_capture_disclosure_version()")
    op.execute("DROP TABLE IF EXISTS kap.disclosures_version")
