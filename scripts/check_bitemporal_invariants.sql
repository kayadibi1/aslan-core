-- Bitemporal invariants checker — pure SQL fallback (H2).
-- Use this when psycopg is not available; mirrors
-- scripts/check_bitemporal_invariants.py invariants.
--
-- Usage:
--   docker exec -i --user postgres <pg_container> \
--     psql -U aslan -d <db_name> -f - < check_bitemporal_invariants.sql
-- The script returns one row per invariant with status ok|fail
-- and the violation count. A summary row at the end reports
-- pass/fail.

\set ON_ERROR_STOP on

WITH inv AS (
    SELECT 'ts_observation_no_duplicate_as_of' AS name,
           (SELECT count(*) FROM (
                SELECT series_id, ts, as_of, count(*) AS c
                FROM ts.observation
                GROUP BY series_id, ts, as_of HAVING count(*) > 1
            ) d) AS violations
    UNION ALL
    SELECT 'ts_observation_as_of_not_null',
           (SELECT count(*) FROM ts.observation WHERE as_of IS NULL)
    UNION ALL
    SELECT 'ts_financial_line_item_no_duplicate_as_of',
           (SELECT count(*) FROM (
                SELECT entity_id, filing_id, statement_type, line_code,
                       consolidation, period_end, as_of, count(*) AS c
                FROM ts.financial_line_item
                GROUP BY entity_id, filing_id, statement_type, line_code,
                         consolidation, period_end, as_of HAVING count(*) > 1
            ) d)
    UNION ALL
    SELECT 'ts_canonical_financial_no_duplicate_as_of',
           (SELECT count(*) FROM (
                SELECT entity_id, canonical_code, period_end, period_type,
                       consolidation, currency_code, accounting_standard,
                       restatement_basis, cpi_base_date, mapping_version,
                       as_of, count(*) AS c
                FROM ts.canonical_financial
                GROUP BY 1,2,3,4,5,6,7,8,9,10,11 HAVING count(*) > 1
            ) d)
    UNION ALL
    SELECT 'agg_filing_event_no_duplicate_as_of',
           (SELECT count(*) FROM (
                SELECT filing_id, event_type, event_seq, as_of, count(*) AS c
                FROM agg.filing_event
                GROUP BY filing_id, event_type, event_seq, as_of HAVING count(*) > 1
            ) d)
    UNION ALL
    SELECT 'ref_identifier_gist_exclude_present',
           (SELECT CASE WHEN EXISTS (
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE n.nspname='ref' AND t.relname='identifier' AND c.contype='x'
            ) THEN 0 ELSE 1 END)
    UNION ALL
    SELECT 'ref_identifier_no_overlapping_pairs',
           (SELECT count(*)::bigint FROM ref.identifier a
                JOIN ref.identifier b
                  ON a.namespace=b.namespace AND a.value=b.value
                 AND a.ctid<>b.ctid
                 AND daterange(a.valid_from, a.valid_to, '[)') &&
                     daterange(b.valid_from, b.valid_to, '[)'))
    UNION ALL
    SELECT 'bitemporal_table_registry_present',
           (SELECT CASE WHEN EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema='aslan_core' AND table_name='bitemporal_table_registry'
            ) THEN 0 ELSE 1 END)
    UNION ALL
    SELECT 'bitemporal_table_registry_completeness',
           (SELECT count(*) FROM (
                SELECT n.nspname AS schema_name, c.relname AS table_name
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_inherits i ON i.inhrelid = c.oid
                WHERE a.attname='as_of'
                  AND a.atttypid='timestamptz'::regtype
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                  AND c.relkind='r'
                  AND i.inhrelid IS NULL  -- exclude inheritance children
                                          -- (TimescaleDB chunks)
                  AND n.nspname NOT IN ('pg_catalog','information_schema',
                                        'aslan_core','audit','streams',
                                        '_timescaledb_internal',
                                        '_timescaledb_catalog',
                                        '_timescaledb_config',
                                        '_timescaledb_cache')
            ) tables
            WHERE NOT EXISTS (
                SELECT 1 FROM aslan_core.bitemporal_table_registry r
                WHERE r.schema_name = tables.schema_name
                  AND r.table_name  = tables.table_name
            ))
    UNION ALL
    SELECT 'bitemporal_trigger_presence',
           (SELECT count(*) FROM aslan_core.bitemporal_table_registry r
                WHERE NOT EXISTS (
                    SELECT 1 FROM pg_trigger t
                    JOIN pg_class c ON c.oid = t.tgrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname=r.schema_name AND c.relname=r.table_name
                      AND t.tgname=r.table_name||'_no_update'
                      AND NOT t.tgisinternal
                ))
)
SELECT name,
       CASE WHEN violations = 0 THEN 'ok' ELSE 'FAIL' END AS status,
       violations
FROM inv
ORDER BY status DESC, name;

-- Summary: caller may grep the per-invariant rows for 'FAIL' to detect
-- a failure. Exit-code semantics handled at the caller / Python wrapper.
