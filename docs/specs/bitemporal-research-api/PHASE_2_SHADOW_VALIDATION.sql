-- Phase 2 shadow validation SQL — bitemporal-research-API.
-- Mirrors alembic revisions 0043-0049 upgrade() bodies for application
-- against a shadow DB that has the prod schema loaded but no
-- alembic_version row. Used for Phase 2g shadow validation per
-- bitemporal-api-prompt.md.
--
-- Apply with:
--   docker exec --user postgres aslan-dashboard-postgres-1 \
--     psql -U aslan -d <shadow_db_name> -f <this-file>
-- Returns 0 errors if the migrations are SQL-valid against the
-- prod schema.
--
-- Generated 2026-05-09 by agent.

\set ON_ERROR_STOP on

BEGIN;

-- =====================================================================
-- 0043: aslan_core schema + bitemporal_table_registry + trigger function
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS aslan_core;

CREATE TABLE aslan_core.bitemporal_table_registry (
    schema_name        TEXT NOT NULL,
    table_name         TEXT NOT NULL,
    entity_columns     TEXT[] NOT NULL,
    as_of_column       TEXT NOT NULL DEFAULT 'as_of',
    pit_function_name  TEXT NOT NULL,
    api_path           TEXT,
    exposed_in_api     BOOLEAN NOT NULL DEFAULT false,
    registered_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes              TEXT,
    PRIMARY KEY (schema_name, table_name)
);

CREATE OR REPLACE FUNCTION aslan_core.reject_bitemporal_update_generic()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'bitemporal table %.% is append-only; UPDATE rejected. '
        'Insert a new (entity, as_of) row instead.',
        TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'feature_not_supported',
              HINT = 'Bitemporal tables advance via INSERT of new '
                     'as_of rows; existing rows are immutable per '
                     'ADR-003 Moat 2.';
END;
$$ LANGUAGE plpgsql;

GRANT USAGE ON SCHEMA aslan_core TO aslan_dashboard;
GRANT SELECT ON aslan_core.bitemporal_table_registry TO aslan_dashboard;

-- =====================================================================
-- 0044: append-only triggers on Class A tables
-- =====================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema='ts' AND table_name='observation') THEN
        EXECUTE $sql$
            CREATE TRIGGER observation_no_update
                BEFORE UPDATE ON ts.observation
                FOR EACH ROW
                EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic()
        $sql$;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema='ts' AND table_name='financial_line_item') THEN
        EXECUTE $sql$
            CREATE TRIGGER financial_line_item_no_update
                BEFORE UPDATE ON ts.financial_line_item
                FOR EACH ROW
                EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic()
        $sql$;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema='ts' AND table_name='canonical_financial') THEN
        EXECUTE $sql$
            CREATE TRIGGER canonical_financial_no_update
                BEFORE UPDATE ON ts.canonical_financial
                FOR EACH ROW
                EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic()
        $sql$;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema='agg' AND table_name='filing_event') THEN
        EXECUTE $sql$
            CREATE TRIGGER filing_event_no_update
                BEFORE UPDATE ON agg.filing_event
                FOR EACH ROW
                EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic()
        $sql$;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema='ref' AND table_name='identifier') THEN
        EXECUTE $sql$
            CREATE TRIGGER identifier_no_update
                BEFORE UPDATE ON ref.identifier
                FOR EACH ROW
                EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic()
        $sql$;
    END IF;
END $$;

INSERT INTO aslan_core.bitemporal_table_registry
    (schema_name, table_name, entity_columns, as_of_column,
     pit_function_name, api_path, exposed_in_api, notes)
VALUES
    ('ts', 'observation', ARRAY['series_id','ts']::text[], 'as_of',
     'ts.observation_at', '/v1/research/observations', false, 'Phase 2'),
    ('ts', 'financial_line_item',
     ARRAY['entity_id','filing_id','statement_type','line_code','consolidation','period_end']::text[],
     'as_of', 'ts.financial_line_item_at', '/v1/research/financials/line-items', false, 'Phase 2'),
    ('ts', 'canonical_financial',
     ARRAY['entity_id','canonical_code','period_end','period_type','consolidation','currency_code','accounting_standard','restatement_basis','cpi_base_date','mapping_version']::text[],
     'as_of', 'ts.canonical_financial_at', '/v1/research/financials/canonical', false, 'Phase 2 — restatement_basis is part of the entity key; as_reported and cpi_normalized coexist as separate logical rows'),
    ('agg', 'filing_event',
     ARRAY['filing_id','event_type','event_seq']::text[],
     'as_of', 'agg.filing_event_at', '/v1/research/events', false, 'Phase 2'),
    ('ref', 'identifier',
     ARRAY['namespace','value']::text[],
     'as_of', 'ref.identifier_at', '/v1/research/identifiers/resolve', false, 'Phase 2 — daterange-based, not as_of-based; PIT function uses daterange @>')
ON CONFLICT DO NOTHING;

-- =====================================================================
-- 0045: NO-OP — ts.canonical_financial.restatement_basis already exists.
-- See migration 0045 docstring for rationale.
-- =====================================================================

-- =====================================================================
-- 0046: ref.entity bitemporal via SCD-4 (history mirror, not replacement)
-- =====================================================================

CREATE TABLE ref.entity_version (
    entity_id              UUID NOT NULL,
    as_of                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_kind             TEXT NOT NULL CHECK (event_kind IN
                               ('created','updated','merged','split','renamed','deleted')),
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
    merged_from_entity_ids JSONB,
    source_id              TEXT NOT NULL,
    ingestion_run_id       BIGINT NOT NULL,
    actor_id               TEXT,
    actor_kind             TEXT,
    client_ip              INET,
    user_agent             TEXT,
    request_id             UUID,
    captured_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, as_of)
);
CREATE INDEX entity_version_entity_idx ON ref.entity_version (entity_id, as_of DESC);
CREATE INDEX entity_version_event_kind_idx ON ref.entity_version (event_kind, as_of DESC);

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
             THEN v_row.metadata->'merged_from_entity_ids' ELSE NULL END,
        v_row.source_id, v_row.ingestion_run_id, v_row.actor_id, v_row.actor_kind,
        v_row.client_ip, v_row.user_agent, v_row.request_id
    )
    ON CONFLICT (entity_id, as_of) DO NOTHING;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER entity_capture_version
    AFTER INSERT OR UPDATE OR DELETE ON ref.entity
    FOR EACH ROW
    EXECUTE FUNCTION ref.fn_capture_entity_version();

CREATE TRIGGER entity_version_no_update
    BEFORE UPDATE ON ref.entity_version
    FOR EACH ROW
    EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic();

-- Backfill from existing rows.
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
ON CONFLICT (entity_id, as_of) DO NOTHING;

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
ON CONFLICT (entity_id, as_of) DO NOTHING;

INSERT INTO aslan_core.bitemporal_table_registry
    (schema_name, table_name, entity_columns, as_of_column,
     pit_function_name, api_path, exposed_in_api, notes)
VALUES
    ('ref', 'entity_version', ARRAY['entity_id']::text[], 'as_of',
     'ref.entity_at', '/v1/research/entities', true,
     'SCD-4 bitemporal history of ref.entity')
ON CONFLICT (schema_name, table_name) DO UPDATE
SET entity_columns = EXCLUDED.entity_columns,
    pit_function_name = EXCLUDED.pit_function_name,
    api_path = EXCLUDED.api_path,
    exposed_in_api = EXCLUDED.exposed_in_api,
    notes = EXCLUDED.notes;

GRANT SELECT ON ref.entity_version TO aslan_dashboard;

-- =====================================================================
-- 0047: ref.entity_lineage table
-- =====================================================================

CREATE TABLE ref.entity_lineage (
    lineage_id          BIGSERIAL PRIMARY KEY,
    event_kind          TEXT NOT NULL CHECK (event_kind IN ('merge','split','rename','continuation')),
    from_entity_id      TEXT NOT NULL,
    into_entity_id      TEXT NOT NULL,
    event_at            TIMESTAMPTZ NOT NULL,
    as_of               TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason              TEXT,
    evidence_filing_id  UUID,
    UNIQUE (event_kind, from_entity_id, into_entity_id, event_at, as_of)
);

CREATE INDEX entity_lineage_from_idx ON ref.entity_lineage (from_entity_id, event_at);
CREATE INDEX entity_lineage_into_idx ON ref.entity_lineage (into_entity_id, event_at);
CREATE INDEX entity_lineage_as_of_idx ON ref.entity_lineage (as_of);

CREATE TRIGGER entity_lineage_no_update
    BEFORE UPDATE ON ref.entity_lineage
    FOR EACH ROW
    EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic();

INSERT INTO aslan_core.bitemporal_table_registry
    (schema_name, table_name, entity_columns, as_of_column,
     pit_function_name, api_path, exposed_in_api, notes)
VALUES
    ('ref', 'entity_lineage',
     ARRAY['from_entity_id','into_entity_id','event_kind','event_at']::text[],
     'as_of', 'ref.entity_lineage_at', NULL, false, 'Auxiliary to ref.entity')
ON CONFLICT (schema_name, table_name) DO UPDATE
SET entity_columns = EXCLUDED.entity_columns,
    pit_function_name = EXCLUDED.pit_function_name,
    notes = EXCLUDED.notes;

GRANT SELECT ON ref.entity_lineage TO aslan_dashboard;

-- =====================================================================
-- 0048: research API support tables
-- =====================================================================

CREATE TABLE aslan_core.api_key (
    key_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    secret_hash         TEXT NOT NULL,
    description         TEXT,
    rate_tier           TEXT NOT NULL DEFAULT 'partner'
                        CHECK (rate_tier IN ('internal','partner','public')),
    rate_overrides      JSONB,
    pii_unredacted      BOOLEAN NOT NULL DEFAULT false,
    scopes              TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL DEFAULT now() + interval '1 year',
    revoked_at          TIMESTAMPTZ,
    last_used_at        TIMESTAMPTZ,
    created_by          TEXT
);
CREATE INDEX api_key_active_filter_idx ON aslan_core.api_key (revoked_at, expires_at);

CREATE TABLE aslan_core.api_query_audit (
    audit_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requested_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    api_key_id            UUID REFERENCES aslan_core.api_key(key_id),
    request_ip            INET,
    request_id            UUID NOT NULL,
    endpoint              TEXT NOT NULL,
    query_params_sha256   BYTEA,
    as_of_requested       TIMESTAMPTZ,
    as_of_resolved        TIMESTAMPTZ NOT NULL,
    rows_returned         BIGINT NOT NULL DEFAULT 0,
    latency_ms            INT NOT NULL,
    status_code           INT NOT NULL,
    error_code            TEXT,
    feature_flags_active  TEXT[] NOT NULL DEFAULT ARRAY[]::text[]
);
CREATE INDEX aqa_requested_at_idx ON aslan_core.api_query_audit (requested_at DESC);
CREATE INDEX aqa_api_key_idx ON aslan_core.api_query_audit (api_key_id, requested_at DESC);
CREATE INDEX aqa_status_idx ON aslan_core.api_query_audit (status_code) WHERE status_code >= 400;

CREATE TABLE aslan_core.api_rate_limit_state (
    api_key_id      UUID NOT NULL REFERENCES aslan_core.api_key(key_id),
    window_kind     TEXT NOT NULL CHECK (window_kind IN ('minute','hour','day')),
    window_start    TIMESTAMPTZ NOT NULL,
    tokens_used     INT NOT NULL DEFAULT 0,
    last_request_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (api_key_id, window_kind, window_start)
);
CREATE INDEX rls_window_idx ON aslan_core.api_rate_limit_state (window_kind, window_start);

CREATE TABLE aslan_core.feature_flags (
    flag_name      TEXT PRIMARY KEY,
    value_text     TEXT,
    value_bool     BOOLEAN,
    value_int      INT,
    scope          TEXT NOT NULL DEFAULT 'global'
                   CHECK (scope IN ('global','environment','api_key')),
    scope_value    TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by     TEXT,
    notes          TEXT
);

INSERT INTO aslan_core.feature_flags (flag_name, value_bool, scope, notes) VALUES
    ('BITEMPORAL_API_ENABLED', false, 'global', 'Master kill-switch'),
    ('BITEMPORAL_API_INTERVAL_QUERIES', true, 'global', 'D2'),
    ('BITEMPORAL_API_ALLOW_NULL_AS_OF', true, 'global', 'D3 + D14'),
    ('BITEMPORAL_API_RATE_LIMIT_ENFORCED', true, 'global', 'D6'),
    ('BITEMPORAL_API_TAS29_RESTATEMENT_CHAIN', false, 'global', 'D8'),
    ('BITEMPORAL_API_ENTITY_MERGE_LINEAGE', false, 'global', 'D10'),
    ('BITEMPORAL_API_KAP_APPEND_ONLY_TRIGGER', false, 'global', 'D11'),
    ('BITEMPORAL_API_AUDIT_LOG_ENABLED', true, 'global', 'D18'),
    ('BITEMPORAL_API_COST_LIMITS_ENFORCED', true, 'global', 'D19'),
    ('BITEMPORAL_API_V2_PREVIEW', false, 'global', 'D23'),
    ('BITEMPORAL_TABLE_REGISTRY_CI_ENFORCED', true, 'global', 'D28'),
    ('BITEMPORAL_API_PII_EXPOSURE', false, 'global', 'D30');

INSERT INTO aslan_core.feature_flags (flag_name, value_text, scope, notes) VALUES
    ('BITEMPORAL_API_CURSOR_VERSION', '1', 'global', 'D7'),
    ('BITEMPORAL_API_ENVELOPE_VERSION', '1', 'global', 'D15'),
    ('BITEMPORAL_API_ERROR_FORMAT_VERSION', '1', 'global', 'D16'),
    ('BITEMPORAL_API_AUTH_ADDITIONAL_SCHEMES', '', 'global', 'D5');

GRANT SELECT ON aslan_core.api_key TO aslan_dashboard;
GRANT SELECT ON aslan_core.api_query_audit TO aslan_dashboard;
GRANT SELECT ON aslan_core.feature_flags TO aslan_dashboard;

-- =====================================================================
-- 0049: PIT functions
-- =====================================================================

CREATE OR REPLACE FUNCTION ts.observation_at(p_as_of TIMESTAMPTZ)
RETURNS SETOF ts.observation
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT DISTINCT ON (series_id, ts) *
    FROM ts.observation
    WHERE as_of <= p_as_of
    ORDER BY series_id, ts, as_of DESC;
$$;

CREATE OR REPLACE FUNCTION ts.financial_line_item_at(p_as_of TIMESTAMPTZ)
RETURNS SETOF ts.financial_line_item
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT DISTINCT ON (entity_id, filing_id, statement_type,
                        line_code, consolidation, period_end) *
    FROM ts.financial_line_item
    WHERE as_of <= p_as_of
    ORDER BY entity_id, filing_id, statement_type, line_code,
             consolidation, period_end, as_of DESC;
$$;

CREATE OR REPLACE FUNCTION ts.canonical_financial_at(p_as_of TIMESTAMPTZ)
RETURNS SETOF ts.canonical_financial
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT DISTINCT ON (entity_id, canonical_code, period_end, period_type,
                        consolidation, currency_code, accounting_standard,
                        restatement_basis, cpi_base_date, mapping_version) *
    FROM ts.canonical_financial
    WHERE as_of <= p_as_of
    ORDER BY entity_id, canonical_code, period_end, period_type,
             consolidation, currency_code, accounting_standard,
             restatement_basis, cpi_base_date, mapping_version, as_of DESC;
$$;

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

CREATE OR REPLACE FUNCTION ref.identifier_at(p_as_of TIMESTAMPTZ)
RETURNS SETOF ref.identifier
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT *
    FROM ref.identifier
    WHERE daterange(valid_from, valid_to, '[)') @> p_as_of::date;
$$;

CREATE OR REPLACE FUNCTION ref.entity_lineage_at(p_as_of TIMESTAMPTZ)
RETURNS SETOF ref.entity_lineage
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT DISTINCT ON (event_kind, from_entity_id, into_entity_id, event_at) *
    FROM ref.entity_lineage
    WHERE as_of <= p_as_of
    ORDER BY event_kind, from_entity_id, into_entity_id, event_at, as_of DESC;
$$;

CREATE OR REPLACE FUNCTION agg.filing_event_at(p_as_of TIMESTAMPTZ)
RETURNS SETOF agg.filing_event
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT DISTINCT ON (filing_id, event_type, event_seq) *
    FROM agg.filing_event
    WHERE as_of <= p_as_of
      AND (superseded_at IS NULL OR superseded_at > p_as_of)
    ORDER BY filing_id, event_type, event_seq, as_of DESC;
$$;

UPDATE aslan_core.bitemporal_table_registry
    SET exposed_in_api = true
    WHERE pit_function_name IN (
        'ts.observation_at',
        'ts.financial_line_item_at',
        'ts.canonical_financial_at',
        'ref.entity_at',
        'ref.identifier_at',
        'ref.entity_lineage_at',
        'agg.filing_event_at',
        'kap.disclosures_at'
    );

-- =====================================================================
-- 0050: ts.entity_quality_score (discovered Phase 2g; same shape as
-- ts.canonical_financial)
-- =====================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema='ts' AND table_name='entity_quality_score') THEN
        EXECUTE $sql$
            CREATE TRIGGER entity_quality_score_no_update
                BEFORE UPDATE ON ts.entity_quality_score
                FOR EACH ROW
                EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic()
        $sql$;
    END IF;
END $$;

INSERT INTO aslan_core.bitemporal_table_registry
    (schema_name, table_name, entity_columns, as_of_column,
     pit_function_name, api_path, exposed_in_api, notes)
VALUES
    ('ts', 'entity_quality_score',
     ARRAY['entity_id','period_end','period_type','consolidation',
           'currency_code','accounting_standard','restatement_basis',
           'cpi_base_date','mapping_version']::text[],
     'as_of', 'ts.entity_quality_score_at',
     '/v1/research/quality-scores', true,
     'Discovered Phase 2g; same shape as ts.canonical_financial')
ON CONFLICT (schema_name, table_name) DO NOTHING;

CREATE OR REPLACE FUNCTION ts.entity_quality_score_at(p_as_of TIMESTAMPTZ)
RETURNS SETOF ts.entity_quality_score
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT DISTINCT ON (entity_id, period_end, period_type,
                        consolidation, currency_code, accounting_standard,
                        restatement_basis, cpi_base_date, mapping_version) *
    FROM ts.entity_quality_score
    WHERE as_of <= p_as_of
    ORDER BY entity_id, period_end, period_type,
             consolidation, currency_code, accounting_standard,
             restatement_basis, cpi_base_date, mapping_version, as_of DESC;
$$;

-- =====================================================================
-- 0051: kap.disclosures bitemporal via SCD-4 (history mirror)
-- =====================================================================

CREATE TABLE kap.disclosures_version (
    disclosure_id          TEXT NOT NULL,
    as_of                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_kind             TEXT NOT NULL CHECK (event_kind IN
                               ('indexed','body_fetched','republished','updated','deleted')),
    as_of_provenance       TEXT NOT NULL DEFAULT 'live'
                           CHECK (as_of_provenance IN
                               ('live','index_fetched_at','body_fetched_at',
                                'published_at','pre_bitemporal_unknown')),
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
);

CREATE INDEX disclosures_version_disclosure_idx ON kap.disclosures_version (disclosure_id, as_of DESC);
CREATE INDEX disclosures_version_event_kind_idx ON kap.disclosures_version (event_kind, as_of DESC);
CREATE INDEX disclosures_version_entity_idx ON kap.disclosures_version (entity_id, as_of DESC);
CREATE INDEX disclosures_version_provenance_idx ON kap.disclosures_version (as_of_provenance) WHERE as_of_provenance <> 'live';

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
        IF NEW.body_fetched IS DISTINCT FROM OLD.body_fetched AND NEW.body_fetched = true THEN
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

CREATE TRIGGER trg_capture_disclosure_version
    AFTER INSERT OR UPDATE OR DELETE ON kap.disclosures
    FOR EACH ROW
    EXECUTE FUNCTION kap.fn_capture_disclosure_version();

CREATE TRIGGER disclosures_version_no_update
    BEFORE UPDATE ON kap.disclosures_version
    FOR EACH ROW
    EXECUTE FUNCTION aslan_core.reject_bitemporal_update_generic();

-- Backfill 'indexed' events from existing rows.
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
    CASE WHEN index_fetched_at IS NOT NULL THEN 'index_fetched_at' ELSE 'pre_bitemporal_unknown' END,
    entity_id, kap_id, published_at, category_code,
    subcategory_code, title, language, kap_url, is_amendment,
    parent_disclosure_id, false, index_fetched_at,
    raw_index_storage_key, NULL,
    NULL, NULL, NULL, NULL
FROM kap.disclosures
ON CONFLICT (disclosure_id, as_of) DO NOTHING;

-- Backfill 'body_fetched' events for the ~89.7k rows.
INSERT INTO kap.disclosures_version (
    disclosure_id, as_of, event_kind, as_of_provenance,
    entity_id, kap_id, published_at, category_code,
    subcategory_code, title, language, kap_url, is_amendment,
    parent_disclosure_id, body_fetched, index_fetched_at,
    raw_index_storage_key, raw_body_storage_key,
    body_fetched_at, raw_body_sha256, raw_body_bytes, raw_body_mime
)
SELECT
    disclosure_id, body_fetched_at, 'body_fetched', 'body_fetched_at',
    entity_id, kap_id, published_at, category_code,
    subcategory_code, title, language, kap_url, is_amendment,
    parent_disclosure_id, true, index_fetched_at,
    raw_index_storage_key, raw_body_storage_key,
    body_fetched_at, raw_body_sha256, raw_body_bytes, raw_body_mime
FROM kap.disclosures
WHERE body_fetched = true AND body_fetched_at IS NOT NULL
ON CONFLICT (disclosure_id, as_of) DO NOTHING;

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

INSERT INTO aslan_core.bitemporal_table_registry
    (schema_name, table_name, entity_columns, as_of_column,
     pit_function_name, api_path, exposed_in_api, notes)
VALUES
    ('kap', 'disclosures_version', ARRAY['disclosure_id']::text[], 'as_of',
     'kap.disclosures_at', '/v1/research/disclosures', true,
     'SCD-4 bitemporal history of kap.disclosures')
ON CONFLICT (schema_name, table_name) DO UPDATE
SET entity_columns = EXCLUDED.entity_columns,
    pit_function_name = EXCLUDED.pit_function_name,
    api_path = EXCLUDED.api_path,
    exposed_in_api = EXCLUDED.exposed_in_api,
    notes = EXCLUDED.notes;

GRANT SELECT ON kap.disclosures_version TO aslan_dashboard;

COMMIT;

-- =====================================================================
-- Smoke check (read-only)
-- =====================================================================

SELECT
    'registry_rows'      AS metric, count(*)::text AS value
FROM aslan_core.bitemporal_table_registry
UNION ALL
SELECT 'feature_flags', count(*)::text FROM aslan_core.feature_flags
UNION ALL
SELECT 'triggers_present',
       count(*)::text
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
WHERE t.tgname LIKE '%_no_update' AND NOT t.tgisinternal
UNION ALL
SELECT 'pit_functions_present',
       count(*)::text
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.proname LIKE '%_at' AND n.nspname IN ('ts','ref','agg','doc','kap');
