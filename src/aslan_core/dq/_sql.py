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
