# CHANGELOG

## v0.3.0 — 2026-04-29 — Audit + Observability Foundation

### Added
- **Actor identity propagation** via `aslan_core.audit.{Actor, set_actor, current_actor, require_actor}`. Actor is set at the request / job boundary; mutations capture it via ContextVar (subtask-safe).
- **Audit columns** (`actor_id`, `actor_kind`, `client_ip`, `user_agent`, `request_id`) on every mutation table — nullable in v0.3, required in v1.0.
- **`audit.events` Timescale hypertable** with row-level before/after JSON snapshots, indexed by actor / target / request / run.
- **`aslan_core.audit.record(...)`** — atomic INSERT in the caller's transaction. Strict-mode rejects mutations without an actor BEFORE any I/O.
- **Fresh-vs-idempotent contract** on every mutation: idempotent retries emit `<table>.idempotent_hit` events and DO NOT rewrite original-creator attribution.
- **Sentry integration** (`setup_sentry`) with disjoint 4-layer `before_send` PII redaction (secret-key names, exact-name filing payloads, sibling-aware AttachmentIn shape, size cap defense-in-depth).
- **OpenTelemetry tracing** (`setup_tracing`, `@traced` decorator) + SQLAlchemy + asyncpg auto-instrumentation. No-op when endpoint is None.
- **Prometheus metrics** with bounded label cardinality: counters (`entity_creates_total`, `filing_puts_total`, `audit_events_total`, `object_storage_orphans_total`, etc.) + histograms (`db_query_duration_seconds`, `object_storage_op_duration_seconds`).
- **`[obs]` install extra** for Sentry / OTel / Prometheus deps.
- **CI security gates**: CodeQL static analysis, pip-audit on locked dependencies, Dependabot, SBOM generation via cyclonedx-py on tagged releases (release.yml).
- **`Settings` hardening**: `SecretStr` for `postgres_dsn`, `s3_access_key`, `s3_secret_key`, `sentry_dsn`. `hide_input_in_errors=True` prevents ConfigError from leaking raw inputs.
- **CLI auto-actor**: `aslan` CLI sets actor from `getuser() + hostname` so manual operations don't need a flag.

### Changed
- `ingestion_run(actor=)` parameter — sets actor for run scope.
- `find_by_source_ref` resolves republished aliases via `metadata->'republished_as'` JSONB containment (carry-over from v0.2 release).

### Migrations
- 0009 — audit columns on every mutation table (nullable).
- 0010 — `audit` schema + `audit.events` hypertable + 4 indexes.

### Codex adversarial review history
4 spec/plan rounds (8 contract bugs caught) + 5 implementation-batch rounds (8 implementation bugs caught). All findings closed with regression tests.
