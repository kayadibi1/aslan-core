# CHANGELOG

## v0.4.0 — 2026-04-29 — Timeseries (ObservationWriter + Reader + ts schema)

### Added
- **`ts.series_catalog`** reference table for series metadata (frequency, unit, currency, restatement_basis, accounting_standard, consolidation, period_type, pii_class, subjects via link table)
- **`ts.observation`** Timescale hypertable with composite PK `(series_id, ts, as_of)` for point-in-time replay
- **`ts.series_subject`** link table for GDPR Art. 17 deletion path (subjects identified per series for identifying-class)
- **`audit.observation_batch_keys`** Timescale hypertable for per-key forensic detail on bulk writes
- **`ObservationWriter` API**: `upsert_series` (idempotent on series_code with race-safe ON CONFLICT), `write` (bulk INSERT-DO-NOTHING with intra-batch + DB conflict detection)
- **`ObservationReader` API**: `get_series` + `latest` + `range` with `DISTINCT ON (series_id, ts)` PIT collapse
- **`aslan_core.timeseries.pii`** module: PiiFinding, find_pii_in_clear_text, find_pii_in_metadata, find_pii_in_metadata_for_subject (subject-aware), path_to_jsonb_set_text_array, has_numeric_string_keys, scrub_in_python — for use by aslan-service Art. 17 deletion runtime
- **`aslan ts` CLI**: series subcommands (upsert, get, list, stats) + observation subcommands (write CSV, latest, range)
- **Bit-preservation contract**: 8-vector round-trip test for IEEE-754 (positive_zero, negative_zero, canonical_qnan, noncanonical_qnan, signaling_nan, smallest_subnormal, +inf, -inf) — all preserve byte-identity through Python+asyncpg+Postgres DOUBLE PRECISION
- **9 new audit operations** in allow-list (series.upsert, series.idempotent_hit, series.update, observation.write_batch, series.subject_erased, series.metadata_pii_scrubbed, series.metadata_bypass_detected, observation.metadata_pii_scrubbed, observation.metadata_bypass_detected)
- **`_KNOWN_FREQUENCIES` allow-list** (13 spec values) for Prometheus label cardinality bounding

### Migrations
- 0011 — `ts.series_catalog` (with audit cols + `pii_class` + 3 indexes)
- 0012 — `ts.observation` Timescale hypertable + 2 indexes + exactly-one CHECK + payload_hash hex CHECK
- 0013 — `ts.series_subject` (Art. 17 deletion path)
- 0014 — `audit.observation_batch_keys` Timescale hypertable + constraint trigger to audit.events (FK to hypertable PK not supported)

### Codex adversarial review history (10 spec/plan rounds + 5 implementation rounds, 28+ contract bugs caught)

Spec/plan review caught 28 contract bugs (F1-F28) including:
- PIT replay correctness (DISTINCT ON ordering)
- Conflict policy on same-key different-payload (immutable identity, restatement = fresh as_of)
- Audit cardinality (intra-batch dedup, audit batch event invariant)
- GDPR contracts (subject-aware scrubbing, structured paths, recursive PII scan, defense-in-depth deletion)
- IEEE-754 bit preservation (NaN/zero/inf vectors with explicit byte-pinned fixtures)
- Concurrency (race-safe upsert via INSERT ON CONFLICT, advisory lock for bulk write)

Implementation review caught additional bugs:
- exactly-one CHECK in observation table
- audit.events FK enforced via constraint trigger
- payload_hash hex CHECK
- batch_size attempted/duplicate_count split
- batch_payload_hash key-binding
- Phase 0 re-validation at write boundary
- pinned-set allow-list test

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
