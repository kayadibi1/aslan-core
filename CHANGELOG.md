# CHANGELOG

## v0.11.0 — 2026-05-08 — `agg.filing_event` schema for aslan-event-extractor M0

### Added

- **`agg.filing_event_type` ENUM** — 32 values, locked by
  `aslan-event-extractor/SCOPE.md` D18. Migration 0034.
- **`agg.filing_event` table + `agg.filing_event_current` view + 6
  indexes**. Bitemporal (`as_of` / `superseded_at`); cross-schema FKs
  to `doc.filing`, `ref.entity`, `src.ingestion_run`. Migration 0035.
- **`agg.filing_event_quarantine`** — bodies the extractor cannot
  process. Migration 0036.
- **`agg.filing_event_review_queue`** — Tier 1/2 disagreements awaiting
  human resolution; partial index `freview_unresolved`. Migration 0037.
- **`agg.filing_event_extraction_state`** — composite PK
  `(filing_id, model_version, prompt_version)` driving re-extraction
  on prompt/model upgrades. Migration 0038.
- **`agg.filing_event_pii_index`** — GDPR Art. 17 PII path index;
  ON DELETE CASCADE on `filing_event_id`. Privileged-only (NOT
  granted to `aslan_dashboard`). Migration 0039.
- **`agg.entity_resolution_queue`** — counterparty / mentioned-entity
  resolution backlog; partial index `erq_pending`. Migration 0040.
- **`agg.extraction_audit_log`** hypertable — every LLM call audited
  for SOC II Processing Integrity; 30-day chunks, compressed after
  90 days; PK is composite `(audit_id, started_at)` (TimescaleDB
  partitioning-column-in-PK requirement). Privileged-only (NOT
  granted to `aslan_dashboard`). Migration 0041.
- **`agg.filing_event_label`** — ground-truth labels for the
  500-disclosure benchmark set; partial index `fel_holdout`.
  Migration 0042.
- **ORM classes** for the 8 new tables in `aslan_core.models.agg`:
  `FilingEvent`, `FilingEventQuarantine`, `FilingEventReviewQueue`,
  `FilingEventExtractionState`, `FilingEventPiiIndex`,
  `EntityResolutionQueue`, `ExtractionAuditLog`, `FilingEventLabel`.

### Notes

- Migrations 0034–0042 are additive; existing schemas unchanged.
- `extraction_audit_log` PK is `(audit_id, started_at)` (composite)
  because TimescaleDB hypertables require the partitioning column in
  the PK. SCOPE §5.3's draft wording showed `audit_id BIGSERIAL PK`
  — the composite is the resolved form.
- `/review` operator route deferred to a follow-up PR (M0 nominally,
  but the existing dashboard's view-model + register_template
  pattern requires its own care to avoid regressing the 30+
  dashboard invariant tests; track as `aslan-event-extractor` M0
  follow-up).

## v0.6.0 — 2026-05-01 — Internal ops dashboard

### Added

- **Read-only operator dashboard** behind the dedicated
  `aslan_dashboard` PostgreSQL role. Nine pages: `/`, `/outbox`,
  `/streams`, `/ingestion`, `/documents`, `/timeseries`,
  `/deadletter`, `/audit`, `/redactions`. POST/PUT/DELETE on every
  route returns 405. The role has `default_transaction_read_only=on`
  and column-allowlist `SELECT` GRANTs only — forbidden columns
  (`outbox.payload`, `filing.body_text`, `audit.events.metadata`,
  `redaction_registry.redacted_payload`, raw `last_error`,
  `client_ip`, `user_agent`) are revoked at the privilege layer.
- **Standing compliance banner** (`Network-edge logging only — not
  compliance evidence`) on `/audit` and `/redactions`.
  Server-rendered, non-dismissible (no client-side JS hooks). Spec
  §6.2: until `aslan-service` ships authenticated per-request
  identity in v0.7.x, the dashboard makes no per-view-attribution
  claim.
- **404 + 500 error handlers**: 404 does NOT echo the requested
  path; 500 emits a fresh UUID `incident_id` and structured-logs
  the exception under that id (no exception text in the rendered
  page).
- **Static asset routes**: three-element allowlist
  (`/static/dashboard.css`, `/static/htmx.min.js`,
  `/static/favicon.ico`). Vendored htmx 1.9.12 with SHA-256 sidecar
  pinned via `<script integrity=...>`. Fixed paths in the route
  table — no path-traversal possible.
- **CLI**: `aslan dashboard serve` with loopback-bind by default;
  non-loopback hosts require `--i-know-this-is-unsafe`. Reads
  `ASLAN_DASHBOARD_DSN`; missing env var exits with a usage error
  naming migration 0020.
- **Bounded Redis probes** with circuit breaker: 200 ms per-call
  timeout, 5-errors-in-60s circuit trip, 30 s open period. Per-page
  budget caps probe count at `1 + len(STREAMS)` so the deadletter
  page renders the same Redis-call count whether the table holds 10
  rows or 100k.
- **Closed-enum metrics**:
  `aslan_dashboard_requests_total{path, status}` with
  `/audit` + `/redactions` collapsed to `path="<sensitive>"`;
  `aslan_dashboard_request_duration_seconds` (no labels — per-route
  timing is a side-channel).
- **Sentinel matrix tests**: every (page × forbidden_field) pair
  seeds a unique sentinel under the privileged role and asserts it
  is absent from the rendered HTML AND the `/metrics` body.
- **Operator runbook**: `docs/dashboard.md` — install,
  localhost vs reverse-proxy deployment, the
  `aslan deadletter inspect` / `aslan ingestion show` /
  `aslan audit show` workflow for inspecting forbidden bytes,
  Redis circuit-breaker recovery.

### Migrations

- 0020 — `aslan_dashboard` role + column-allowlist GRANTs +
  `audit.event_metadata_key_count` SECURITY DEFINER helper.
- 0021 — `aslan_app` SELECT on `audit.events.metadata` (companion
  for the v0.6.0 helper).
- 0022 — REVOKE raw `client_ip` + `user_agent` from
  `aslan_dashboard` on 4 tables; `audit.event_client_ip_truncated`
  SECURITY DEFINER helper for the rendered CIDR.
- 0023 — REVOKE `metadata` SELECT from `aslan_dashboard` on
  `src.ingestion_run` + `doc.filing`; static-SQL scanner allowlist
  tightened with two new forbidden-column pairs (codex post-impl
  HIGH).
- 0024 — Dedicated NOLOGIN `aslan_audit_helpers` role owns both
  `audit.event_metadata_key_count` + `audit.event_client_ip_truncated`
  SECURITY DEFINER helpers; raw `metadata` and `client_ip` SELECT
  REVOKEd from `aslan_app`; helper-owner-isolated test pins the
  contract (codex post-impl HIGH).

### Codex adversarial review history

Forty-four findings absorbed across four phases (34 spec/plan + 5
branch-state + 3 post-implementation + 2 ultrareview).

**Spec/plan + branch-state highlights:**

- F-1 CRITICAL: raw `client_ip` / `user_agent` were granted to the
  dashboard role; revoked in migration 0022 + truncated CIDR helper.
- F-2 HIGH: scanner walked only module-scope imports; rewritten to
  walk every `Import` / `ImportFrom` with 4 EVIL_STUBS for
  function-local imports.
- F-3 MEDIUM: column-allowlist enforcement on `queries.py` text()
  literals via sqlglot's PostgreSQL dialect.
- F-4 MEDIUM: parallel test runs every query helper under a real
  LOGIN-as-aslan_dashboard SQLAlchemy engine.
- F-5 MEDIUM: lint rejects f-string `op.execute` whose source
  contains `SECURITY DEFINER`.

**Post-implementation review (3 findings):**

- HIGH: `metadata` was still readable by `aslan_dashboard` on
  `src.ingestion_run` + `doc.filing` (the dashboard never selected
  it, but the GRANT layer permitted it) — closed by migration 0023.
- HIGH: SECURITY DEFINER audit helpers ran as `aslan_app`, so
  `aslan_app` retained a transitive read path to raw `metadata` and
  `client_ip` — closed by migration 0024 (dedicated NOLOGIN owner
  role).
- MEDIUM: `prometheus-client` was an undeclared transitive of
  `python-fasthtml`; pinned `>=0.21` in the `[dashboard]` extra so
  `/metrics` cannot silently empty under a documented install.

**Ultrareview (2 in-scope findings — full repo squashed snapshot
review):**

- bug_003 (normal): `StreamRowVM.xlen: int | None`; failed Redis
  probes now render as `?` instead of being collapsed to `0` (which
  was indistinguishable from a successfully empty stream).
- bug_005 (nit): dropped the broken `--reload` flag from `aslan
  dashboard serve` and the operator runbook — uvicorn's
  `--reload` requires an import string + worker startup hook that
  v0.6.0 does not ship, so the flag exited with a warning when used.

### Compatibility

- No public API changes outside the new `aslan_core.dashboard`
  package and `aslan dashboard` CLI. Existing consumers continue to
  import from `streams`, `documents`, `registry`, `timeseries`,
  `audit`, `cli` unchanged.
- The `[dashboard]` extra is opt-in
  (`uv pip install -e '.[dashboard]'`); base installs do not pull
  `python-fasthtml` or `uvicorn`.

## v0.5.0 — 2026-04-29 — Streams (StreamProducer + outbox + StreamConsumer + GDPR redaction)

### Added
- **`streams` schema** owned by aslan-core's Alembic; six tables shipped:
  - `streams.outbox` — pending Redis Stream writes (drainer-owned, unique on event_id)
  - `streams.event_id_to_redis` — drainer-populated `(event_id, stream_name) → redis_message_id` index for crash-safe exactly-once-observable delivery (codex F3)
  - `streams.deadletter_log` — durable Postgres-first row per stuck message; unique on `(stream, group, original_message_id)` for ON CONFLICT DO UPDATE idempotency (codex F18)
  - `streams.deadletter_redis_index` — `(failure_id → redis_message_id)` mapping (codex F9)
  - `streams.deadletter_xadd_intent` — pre-XADD intent rows for `acquire_or_adopt_intent` (codex F21+F22)
  - `streams.redaction_registry` — GDPR Art. 17 redaction registry; direct mutation REVOKEd from `aslan_app`; SECURITY DEFINER function `streams.redaction_registry_insert` is the only mutation surface (codex F15 round 6)
- **`StreamProducer` API**: transactional outbox INSERT in the caller's session; auto-stamps `producer_run_id`, `actor_id`, `actor_kind`, `traceparent` BEFORE any DB I/O; strict-mode rejects publish without an actor (codex F3)
- **`drain_outbox` daemon**: long-running drainer with `SELECT … FOR UPDATE SKIP LOCKED`; UPDATE outbox + INSERT into `event_id_to_redis` in the SAME transaction so a crash between them leaves the row pending (codex critical-contract item 6)
- **`StreamConsumer` API**: XREADGROUP-driven async iterator with split claim/processed dedup contract (codex F1 + F-impl-1), XAUTOCLAIM crash-recovery (codex F16), schema-version gate with route-to-dead-letter on first occurrence (codex spec §4), OTel link propagation from producer traceparent (codex spec §10)
- **Dead-letter routing protocol** (`route_to_deadletter`, `acquire_or_adopt_intent`, `find_orphan_in_deadletter_stream`, `stream_deadletter_janitor`): durable Postgres-first 6-step routing with bounded re-entry depth (codex F22), paginated XRANGE walk (codex F19), 4-pass janitor (codex F17)
- **GDPR Art. 17 helpers** (`write_registry_entry`, `acquire_event_lock`, `canonical_payload_hash`, `redaction_lock_key`): per-event `pg_advisory_xact_lock` blocks concurrent redaction during consumer yield (codex F13); `aslan_app` role can SELECT from registry but only mutate via the SECURITY DEFINER function (codex F15)
- **`aslan streams` CLI**: 10 subcommands (publish, tail, drain, lag, pending, deadletter list/retry, claim-release, processed-clear, known) — operator-facing surface for the streams subsystem
- **5 canonical event schemas**: `FilingNewEvent`, `FilingAmendedEvent`, `ObservationBatchEvent`, `EntityCreatedEvent`, `StreamEntryRedactedEvent` — discriminated-union `KnownStreamEvent` Pydantic-v2 models, all `frozen=True`, tz-aware UTC
- **Canonical stream-name constants** (`STREAMS`, `STREAM_FOR_EVENT_KIND`, `PII_BEARING_STREAMS`) — frozen MappingProxyType views; mirrored on the `_KNOWN_STREAMS` Prometheus allow-list (codex spec §9)
- **9 new audit operations** in the allow-list: `stream.publish`, `stream.outbox_drained`, `stream.consume_ack`, `stream.consumed_redacted`, `stream.deadletter`, `stream.deadletter_orphan_lost`, `stream.deadletter_orphan_lost_recovered`, `stream.deadletter_orphan_reconciled`, `stream.deadletter_orphan_xdel`, `stream.deadletter_index_orphaned_in_redis`, `stream.entry_redacted`
- **Pinned-set tests** for `_KNOWN_AUDIT_OPERATIONS`, `_KNOWN_STREAMS`, `_KNOWN_CONSUMER_GROUPS` to prevent silent cardinality drift
- **F-headline regression tests**: F1 (split claim/processed), F4 (caller exception leaves PEL intact), F13 (advisory lock blocks concurrent redaction), F15 (aslan_app cannot direct-INSERT registry), F18 (ON CONFLICT idempotency), F19 (paginated XRANGE), F20 (fresh other-owner aborts), F21 (stale adoption reconciles orphan), F22 (bounded re-entry storm raises StreamRoutingContention)

### Migrations
- 0015 — `streams` schema
- 0016 — `streams.outbox` + indexes
- 0017 — `streams.deadletter_log` + `deadletter_redis_index` + `deadletter_xadd_intent`
- 0018 — `streams.redaction_registry` + `aslan_app` role + SECURITY DEFINER function + REVOKE/GRANT
- 0019 — `streams.event_id_to_redis` (drainer-populated index)

### Codex adversarial review history (multiple spec/plan rounds + 3 implementation rounds)

Spec/plan review caught 22 contract bugs (F1–F22) including:
- Split claim/processed dedup contract for drainer-retry duplicates (F1, F-impl-1)
- ON CONFLICT idempotency on the durable deadletter_log row (F18)
- Postgres-first 6-step routing protocol with crash-safe XADD ↔ index INSERT atomicity (F5, F9)
- `acquire_or_adopt_intent` multi-step adoption with bounded re-entry depth (F21, F22)
- Paginated XRANGE walk in the orphan-finder (F19)
- Per-event `pg_advisory_xact_lock` to block concurrent redaction during consumer yield (F13)
- `aslan_app` role isolation: SECURITY DEFINER function as the only registry mutation surface (F15 round 6)
- 4-pass dead-letter janitor: stale intent reconciliation, stuck row re-drive, index-to-stream verification, stream-to-index defense-in-depth (F17)

Implementation review caught additional bugs:
- Loser-XACK contract on duplicate event_ids (F-impl-1) — the second consumer XACKs the duplicate without yielding
- Drainer crash-after-XADD idempotency proven via dedicated 10x-retry test
- Cardinality drift prevention via pinned-set tests for streams + consumer groups

### Credit
- Codex implemented Tasks 12-18 (StreamConsumer + dead-letter routing + janitor + GDPR redaction) over the rate-limit window.

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
