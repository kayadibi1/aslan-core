-- Reverse of PHASE_2_SHADOW_VALIDATION.sql.
-- Drops everything migrations 0043-0051 created, in reverse dependency order.
-- Used for the Phase 2h reversibility test on shadow.
--
-- Idempotent (uses IF EXISTS); safe to apply twice.

\set ON_ERROR_STOP on

BEGIN;

-- ===== 0051 down =====
DELETE FROM aslan_core.bitemporal_table_registry WHERE schema_name='kap' AND table_name='disclosures_version';
DROP FUNCTION IF EXISTS kap.disclosures_at(TIMESTAMPTZ);
DROP TRIGGER IF EXISTS trg_capture_disclosure_version ON kap.disclosures;
DROP FUNCTION IF EXISTS kap.fn_capture_disclosure_version();
REVOKE SELECT ON kap.disclosures_version FROM aslan_dashboard;
DROP TABLE IF EXISTS kap.disclosures_version;

-- ===== 0050 down =====
DROP FUNCTION IF EXISTS ts.entity_quality_score_at(TIMESTAMPTZ);
DELETE FROM aslan_core.bitemporal_table_registry WHERE schema_name='ts' AND table_name='entity_quality_score';
DROP TRIGGER IF EXISTS entity_quality_score_no_update ON ts.entity_quality_score;

-- ===== 0049 down =====
DROP FUNCTION IF EXISTS ts.observation_at(TIMESTAMPTZ);
DROP FUNCTION IF EXISTS ts.financial_line_item_at(TIMESTAMPTZ);
DROP FUNCTION IF EXISTS ts.canonical_financial_at(TIMESTAMPTZ);
DROP FUNCTION IF EXISTS ref.identifier_at(TIMESTAMPTZ);
DROP FUNCTION IF EXISTS ref.entity_lineage_at(TIMESTAMPTZ);
DROP FUNCTION IF EXISTS agg.filing_event_at(TIMESTAMPTZ);

-- ===== 0048 down =====
REVOKE SELECT ON aslan_core.feature_flags FROM aslan_dashboard;
REVOKE SELECT ON aslan_core.api_query_audit FROM aslan_dashboard;
REVOKE SELECT ON aslan_core.api_key FROM aslan_dashboard;
DROP TABLE IF EXISTS aslan_core.feature_flags;
DROP TABLE IF EXISTS aslan_core.api_rate_limit_state;
DROP TABLE IF EXISTS aslan_core.api_query_audit;
DROP TABLE IF EXISTS aslan_core.api_key;

-- ===== 0047 down =====
REVOKE SELECT ON ref.entity_lineage FROM aslan_dashboard;
DELETE FROM aslan_core.bitemporal_table_registry WHERE schema_name='ref' AND table_name='entity_lineage';
DROP TRIGGER IF EXISTS entity_lineage_no_update ON ref.entity_lineage;
DROP TABLE IF EXISTS ref.entity_lineage;

-- ===== 0046 down (SCD-4) =====
REVOKE SELECT ON ref.entity_version FROM aslan_dashboard;
DELETE FROM aslan_core.bitemporal_table_registry WHERE schema_name='ref' AND table_name='entity_version';
DROP FUNCTION IF EXISTS ref.entity_at(TIMESTAMPTZ);
DROP TRIGGER IF EXISTS entity_capture_version ON ref.entity;
DROP FUNCTION IF EXISTS ref.fn_capture_entity_version();
DROP TRIGGER IF EXISTS entity_version_no_update ON ref.entity_version;
DROP TABLE IF EXISTS ref.entity_version;

-- ===== 0045 down (no-op) =====
-- (nothing)

-- ===== 0044 down =====
DROP TRIGGER IF EXISTS observation_no_update ON ts.observation;
DROP TRIGGER IF EXISTS financial_line_item_no_update ON ts.financial_line_item;
DROP TRIGGER IF EXISTS canonical_financial_no_update ON ts.canonical_financial;
DROP TRIGGER IF EXISTS filing_event_no_update ON agg.filing_event;
DROP TRIGGER IF EXISTS identifier_no_update ON ref.identifier;
DELETE FROM aslan_core.bitemporal_table_registry
    WHERE (schema_name, table_name) IN (
        ('ts','observation'), ('ts','financial_line_item'),
        ('ts','canonical_financial'), ('agg','filing_event'),
        ('ref','identifier')
    );

-- ===== 0043 down =====
REVOKE SELECT ON aslan_core.bitemporal_table_registry FROM aslan_dashboard;
REVOKE USAGE ON SCHEMA aslan_core FROM aslan_dashboard;
DROP FUNCTION IF EXISTS aslan_core.reject_bitemporal_update_generic();
DROP TABLE IF EXISTS aslan_core.bitemporal_table_registry;
-- Drop the schema only if empty.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_schema='aslan_core'
        UNION ALL
        SELECT 1 FROM information_schema.routines WHERE routine_schema='aslan_core'
    ) THEN
        EXECUTE 'DROP SCHEMA aslan_core';
    END IF;
END $$;

COMMIT;

-- Smoke check (read-only)
SELECT count(*) AS aslan_core_tables_remaining
FROM information_schema.tables WHERE table_schema='aslan_core';
SELECT count(*) AS bitemporal_triggers_remaining
FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
WHERE t.tgname LIKE '%_no_update' AND NOT t.tgisinternal;
SELECT count(*) AS pit_functions_remaining
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE p.proname LIKE '%_at' AND n.nspname IN ('ts','ref','agg','doc','kap');
