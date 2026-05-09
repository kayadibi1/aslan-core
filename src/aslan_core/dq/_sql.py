"""All SQL fragments used by the dq client live here.

Centralized so the dashboard's static-SQL scanner
(src/aslan_core/dashboard/_static_sql_scan.py) can lint these too,
and so a future migration that renames a column lights up exactly
one place to change.
"""

from __future__ import annotations

from sqlalchemy import text

INSERT_SYNC_LOG_START = text(
    "INSERT INTO audit.sync_log("
    "  source, operation, started_at, status, "
    "  records_ingested, records_failed, "
    "  upstream_max_at, db_max_at, "
    "  error_summary, git_sha, host"
    ") VALUES ("
    "  :source, :operation, :started_at, 'running', "
    "  NULL, NULL, NULL, NULL, NULL, :git_sha, :host"
    ") RETURNING sync_id"
)

UPDATE_SYNC_LOG_FINISH = text(
    "UPDATE audit.sync_log SET "
    "  completed_at = :completed_at, "
    "  status = :status, "
    "  records_ingested = :records_ingested, "
    "  records_failed = :records_failed, "
    "  upstream_max_at = :upstream_max_at, "
    "  db_max_at = :db_max_at, "
    "  error_summary = :error_summary "
    "WHERE sync_id = :sync_id"
)

INSERT_VALIDATION_FAILURE = text(
    "INSERT INTO audit.validation_failure("
    "  source, rule_name, severity, record_table, record_pk, "
    "  detected_at, detail"
    ") VALUES ("
    "  :source, :rule_name, :severity, :record_table, "
    "  CAST(:record_pk AS JSONB), :detected_at, CAST(:detail AS JSONB)"
    ") RETURNING failure_id, recorded_at"
)

INSERT_COVERAGE_SNAPSHOT = text(
    "INSERT INTO audit.coverage_snapshot("
    "  source, dimension, observed_at, expected_count, actual_count, "
    "  missing_ids, target_pct"
    ") VALUES ("
    "  :source, :dimension, :observed_at, :expected_count, :actual_count, "
    "  CAST(:missing_ids AS JSONB), :target_pct"
    ") ON CONFLICT (source, dimension, observed_at) DO NOTHING "
    "RETURNING snapshot_id"
)

INSERT_EVENT = text(
    "INSERT INTO audit.event("
    "  event_type, emitted_at, emitter, severity, payload"
    ") VALUES ("
    "  :event_type, :emitted_at, :emitter, :severity, CAST(:payload AS JSONB)"
    ") RETURNING event_id"
)

INSERT_RECENCY_OBSERVATION = text(
    "INSERT INTO audit.recency_observation("
    "  source, observed_at, upstream_latest_at, db_latest_at, "
    "  sla_target_seconds, probe_detail"
    ") VALUES ("
    "  :source, :observed_at, :upstream_latest_at, :db_latest_at, "
    "  :sla_target_seconds, CAST(:probe_detail AS JSONB)"
    ") RETURNING observation_id, sla_breached, lag_seconds"
)

SELECT_RECENCY_SLA = text(
    "SELECT source, dimension, sla_seconds FROM audit.recency_sla ORDER BY source, dimension"
)

# ── dq M2: spot-check workflow ───────────────────────────────────


INSERT_SPOT_CHECK_SAMPLE = text(
    "INSERT INTO audit.spot_check_sample("
    "  source, drawn_at, record_table, record_pk, stratum"
    ") VALUES ("
    "  :source, :drawn_at, :record_table, CAST(:record_pk AS JSONB), :stratum"
    ") RETURNING sample_id"
)

INSERT_SPOT_CHECK_RESULT = text(
    "INSERT INTO audit.spot_check_result("
    "  sample_id, field, db_value, truth_value, variance_pct, "
    "  matches, label_note, labeller, label_event_id"
    ") VALUES ("
    "  :sample_id, :field, :db_value, :truth_value, :variance_pct, "
    "  :matches, :label_note, :labeller, :label_event_id"
    ") RETURNING result_id"
)

UPDATE_SPOT_CHECK_SAMPLE_LABELLED = text(
    "UPDATE audit.spot_check_sample SET "
    "  labelled = true, "
    "  labelled_at = :labelled_at, "
    "  labeller = :labeller "
    "WHERE sample_id = :sample_id AND labelled = false"
)

SELECT_SPOT_CHECK_SAMPLE_BY_ID = text(
    "SELECT sample_id, source, drawn_at, record_table, record_pk, "
    "  stratum, labelled, labelled_at, labeller, recorded_at "
    "FROM audit.spot_check_sample WHERE sample_id = :sample_id"
)


# ── dq M4: bloomberg-comparison framework ────────────────────────


SELECT_BLOOMBERG_RUN_BY_QUARTER = text(
    "SELECT run_id, quarter, opened_at, closed_at "
    "FROM audit.bloomberg_comparison_run WHERE quarter = :quarter"
)

INSERT_BLOOMBERG_RUN = text(
    "INSERT INTO audit.bloomberg_comparison_run(quarter) VALUES (:quarter) RETURNING run_id"
)

INSERT_BLOOMBERG_CELL = text(
    "INSERT INTO audit.bloomberg_comparison_cell("
    "  run_id, entity_ticker, field"
    ") VALUES (:run_id, :entity_ticker, :field) "
    "ON CONFLICT (run_id, entity_ticker, field) DO NOTHING "
    "RETURNING cell_id"
)

SELECT_BLOOMBERG_CELL_BY_ID = text(
    "SELECT cell_id, run_id, entity_ticker, field, bloomberg_value, "
    "  bloomberg_entered_by, bloomberg_entered_at, aslan_value, "
    "  aslan_sampled_at, variance_pct, aslan_advantage "
    "FROM audit.bloomberg_comparison_cell WHERE cell_id = :cell_id"
)

UPDATE_BLOOMBERG_CELL_BLOOMBERG_VALUE = text(
    "UPDATE audit.bloomberg_comparison_cell SET "
    "  bloomberg_value = :bloomberg_value, "
    "  bloomberg_entered_by = :entered_by, "
    "  bloomberg_entered_at = now() "
    "WHERE cell_id = :cell_id"
)

UPDATE_BLOOMBERG_CELL_ASLAN_VALUE = text(
    "UPDATE audit.bloomberg_comparison_cell SET "
    "  aslan_value = :aslan_value, "
    "  aslan_sampled_at = now(), "
    "  variance_pct = :variance_pct, "
    "  aslan_advantage = :aslan_advantage "
    "WHERE cell_id = :cell_id"
)

SELECT_BLOOMBERG_NULL_CELL_COUNT = text(
    "SELECT count(*)::int AS n FROM audit.bloomberg_comparison_cell "
    "WHERE run_id = :run_id AND bloomberg_value IS NULL"
)

UPDATE_BLOOMBERG_RUN_CLOSE = text(
    "UPDATE audit.bloomberg_comparison_run SET "
    "  closed_at = now(), closed_by = :closed_by "
    "WHERE run_id = :run_id AND closed_at IS NULL"
)


# ── dq M5: regression_flag ───────────────────────────────────────


INSERT_REGRESSION_FLAG = text(
    "INSERT INTO audit.regression_flag("
    "  source, record_table, record_pk, metric, "
    "  prior_value, current_value, shift_pct, threshold_pct, detected_at"
    ") VALUES ("
    "  :source, :record_table, CAST(:record_pk AS JSONB), :metric, "
    "  :prior_value, :current_value, :shift_pct, :threshold_pct, :detected_at"
    ") RETURNING flag_id"
)

UPDATE_REGRESSION_FLAG_STATUS = text(
    "UPDATE audit.regression_flag SET "
    "  status = :status, "
    "  reviewer = :reviewer, "
    "  reviewed_at = now(), "
    "  review_note = :review_note "
    "WHERE flag_id = :flag_id"
)

SELECT_REGRESSION_FLAG_BY_ID = text(
    "SELECT flag_id, source, record_table, record_pk, metric, "
    "  prior_value, current_value, shift_pct, threshold_pct, "
    "  detected_at, status, reviewer, reviewed_at, review_note, recorded_at "
    "FROM audit.regression_flag WHERE flag_id = :flag_id"
)

SELECT_REGRESSION_FLAGS_OPEN = text(
    "SELECT flag_id, source, record_table, record_pk, metric, "
    "  prior_value, current_value, shift_pct, threshold_pct, "
    "  detected_at, status, reviewer, reviewed_at, review_note, recorded_at "
    "FROM audit.regression_flag "
    "WHERE status = 'open' "
    "ORDER BY detected_at DESC "
    "LIMIT :limit"
)


# ── dq M6: scorecard_snapshot ────────────────────────────────────


UPSERT_SCORECARD_SNAPSHOT = text(
    "INSERT INTO audit.scorecard_snapshot("
    "  week_start, metric_name, target, actual, status, notes"
    ") VALUES ("
    "  :week_start, :metric_name, :target, :actual, :status, :notes"
    ") ON CONFLICT (week_start, metric_name) DO UPDATE SET "
    "  actual = EXCLUDED.actual, "
    "  status = EXCLUDED.status, "
    "  notes = EXCLUDED.notes"
)

SELECT_SCORECARD_SNAPSHOT_BY_WEEK = text(
    "SELECT week_start, metric_name, target, actual, status, notes, recorded_at "
    "FROM audit.scorecard_snapshot "
    "WHERE week_start = :week_start "
    "ORDER BY metric_name"
)

SELECT_SCORECARD_SNAPSHOT_WEEKS = text(
    "SELECT week_start, "
    "  count(*) FILTER (WHERE status = 'pass')::int AS pass_count, "
    "  count(*) FILTER (WHERE status = 'warn')::int AS warn_count, "
    "  count(*) FILTER (WHERE status = 'fail')::int AS fail_count, "
    "  count(*)::int AS total_count, "
    "  max(recorded_at) AS recorded_at "
    "FROM audit.scorecard_snapshot "
    "GROUP BY week_start "
    "ORDER BY week_start DESC "
    "LIMIT :limit"
)


# ── dq NG6: external_corroborator_cache ──────────────────────────


INSERT_CORROBORATOR_CACHE = text(
    "INSERT INTO audit.external_corroborator_cache("
    "  source, entity_ticker, fetched_at, cached_payload, "
    "  fetch_url, fetch_latency_ms, fetch_status, error_summary"
    ") VALUES ("
    "  :source, :entity_ticker, :fetched_at, CAST(:cached_payload AS JSONB), "
    "  :fetch_url, :fetch_latency_ms, :fetch_status, :error_summary"
    ") RETURNING cache_id"
)

SELECT_CORROBORATOR_LATEST = text(
    "SELECT cache_id, source, entity_ticker, fetched_at, cached_payload, "
    "  fetch_url, fetch_latency_ms, fetch_status, error_summary, recorded_at "
    "FROM audit.external_corroborator_cache "
    "WHERE source = :source AND entity_ticker = :entity_ticker "
    "ORDER BY fetched_at DESC LIMIT 1"
)
