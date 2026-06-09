# Changelog — Bitemporal Research API

All notable changes to the Aslan Terminal Bitemporal Research API are
recorded here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning 2.0.0](https://semver.org/).

The deployed version is reported by `GET /v1/research/version` and as
`served_by: bitemporal-api/<commit-sha>` in every response envelope.

---

## [v1.0.0-alpha.6] — 2026-05-09 (round 6: bug-review sweep)

Closes the comprehensive bug review run by Codex (gpt-5.5 +
xhigh) against the `BUG_REVIEW.md` spec. **17/17 findings
resolved** (1 BLOCKER, 4 HIGH, 6 MEDIUM, 6 LOW).

### Fixed

- **BLOCKER** — `scripts/check_bitemporal_invariants.py`:
  the `ref_identifier_no_overlapping_pairs` invariant referenced
  nonexistent `a.daterange` / `b.daterange` columns; replaced
  with `daterange(a.valid_from, a.valid_to, '[)') &&
  daterange(b.valid_from, b.valid_to, '[)')`. The H2 invariant
  gate now resolves on the live schema.
- **HIGH (round-5 regression)** — research RFC 7807 exception
  handlers were registered before the generic handlers; FastAPI
  keeps one handler per exception class, so the generic was
  overwriting the research envelope. Reordered: generic first,
  research second so research wins.
- **HIGH** — `/entities`, `/entities/{id}`, `/disclosures`,
  `/disclosures/{id}` now query `*_at(:as_of)` PIT functions
  (added in migrations 0046 and 0051) instead of the underlying
  current-state tables.
- **HIGH** — `disclosure_id` path parameter typed as `str` (was
  `UUID`); KAP IDs (`KAP-2024-1234567`) now path-valid.
- **HIGH** — disclosure SQL aliases `parent_disclosure_id AS
  republished_as` to match the OpenAPI contract.
- `_PUBLIC_PATHS` is now an exact-match `frozenset` of full
  paths; suffix matching let a future `/v1/research/admin/version`
  bypass auth.
- Audit middleware uses `path.startswith("/v1/research/")`
  instead of substring matching.
- Compose canary entrypoint shell vars escaped as
  `$${BITEMPORAL_CANARY_INTERVAL_SECONDS:-300}` so the in-container
  shell expands them at runtime (was Compose-time, often blank,
  killing the loop after one iteration).
- CI workflow trigger paths cover `research_observability.py`,
  `research_logging.py`, all 0043-0051 migrations, the SQL
  invariant fallback, and the new tests; lint + mypy steps cover
  the same files; added a `collect-tests` job.

### Added

- **D2 interval mode** — every list endpoint accepts
  `?as_of_range=[T1,T2)` (mutually exclusive with `as_of`). New
  helpers `_parse_as_of_range`, `_enforce_query_cost`,
  `_reject_pit_with_interval`. Width > 5y or page > 500 → 413
  `QUERY_TOO_LARGE`. Interval-mode SQL routes through SCD-4
  history tables (`ref.entity_version`, `kap.disclosures_version`)
  to return the version chain.
- **`series_code` resolver** on `/observations` — accepts string
  catalog IDs (`BIST.GARAN.close`); resolved against
  `ts.series_catalog`. Direct numeric `series_id` still accepted.
- `RequestContext.as_of_range` field; request-completed log line
  surfaces it.
- `tests/research/test_round6.py` — 12 new integration cases
  covering `as_of_range` parser variants, `QUERY_TOO_LARGE`,
  `as_of`/`as_of_range` mutual exclusion, `disclosure_id: str`
  path parameter, RFC 7807 handler ordering (catches the round-5
  regression), `_PUBLIC_PATHS` exact-match.

### Changed

- **Response shape alignment with OpenAPI:**
  - `/entities` returns `entity_id, canonical_name, kind,
    country, merged_from_entity_ids, split_from_entity_id, as_of,
    lineage_events`.
  - `/events` renamed `filing_event_id` → `event_id`,
    `event_ts` → `occurred_at`, `payload` → `attributes`.
  - `/disclosures` adds `kap_company_id`, `form_type`,
    `is_amendment`, `as_of`, `as_of_provenance`,
    `pre_bitemporal`, `event_kind`.
  - OpenAPI schemas (`Entity`, `Disclosure`, `FilingEvent`,
    `Observation`) updated to match; validator passes.
- `/observations` query parameters renamed:
  `obs_from`/`obs_to` → `ts_from`/`ts_to`.
- `/disclosures` no longer carries the `PRE_BITEMPORAL_TABLE`
  envelope warning (the table IS bitemporal post-0051);
  per-row `as_of_provenance` enum carries provenance instead.
  The `PRE_BITEMPORAL_TABLE` warning now applies to `/filings`
  (Class F; no `doc.filing_at` PIT function in v1).
- README `/v1/verify/moat-2` → `/v1/research/verify/moat-2`.
- RUNBOOK migration range `0043-0050` → `0043-0051`.

### Verification

- ruff + mypy --strict clean across the bitemporal-API surface.
- openapi-spec-validator PASS on the round-6 OpenAPI changes.
- Phase 2g re-validation on `aslan_shadow_round6_1778302913`:
  9 registry rows, 9 triggers, 9 PIT functions, 16 flags;
  `check_bitemporal_invariants.sql` returns 10/10 PASS.
- `pytest --collect-only -m integration tests/research/` →
  **48 tests** (was 35).

---

## [v1.0.0-alpha] — 2026-05-09

First internal-only release of the bitemporal read API. Schema
migrations applied to shadow + staging only; production stays gated
by `BITEMPORAL_API_ENABLED=false` until Phase 7e per
[SCOPE.md D27](./SCOPE.md#d27--deployment-shape).

### Added

- **Schema migrations 0043-0050** under `src/aslan_core/db/migrations/`:
  - `0043_aslan_core_bitemporal_registry` — `aslan_core` schema +
    `bitemporal_table_registry` table + `reject_bitemporal_update_generic()`
    trigger function (per H1).
  - `0044_append_only_triggers_class_a` — BEFORE UPDATE triggers on
    `ts.observation`, `ts.financial_line_item`, `ts.canonical_financial`,
    `agg.filing_event`, `ref.identifier`.
  - `0045_canonical_financial_restatement_kind` — verified existing
    `restatement_basis` covers TAS 29 chain (effective no-op).
  - `0046_ref_entity_bitemporal_upgrade` — implemented as SCD-4
    (current-state pointer + `ref.entity_version` history mirror).
    AFTER INSERT/UPDATE/DELETE trigger captures every change with
    `event_kind in (created, updated, merged, split, renamed,
    deleted)`. Backfill from existing rows. PIT function
    `ref.entity_at(p_as_of)`. Preserves all 14+ FK references to
    `ref.entity_pkey`.
  - `0047_ref_entity_lineage` — new bitemporal merge/split table.
  - `0048_research_api_support_tables` — `aslan_core.api_key`,
    `api_query_audit`, `api_rate_limit_state`, `feature_flags`
    (16 flags seeded conservative-by-default).
  - `0049_pit_functions` — 6 PIT SQL functions over the registered
    Class A tables.
  - `0050_entity_quality_score_bitemporal` — 7th Class A table
    discovered during Phase 2g shadow validation.
- **API endpoints** under `/v1/research/`:
  - `GET /healthz` (public).
  - `GET /version` (public; feature-flag snapshot).
  - `GET /verify/moat-2` (public; D20 verifiability endpoint).
  - `GET /observations` — `ts.observation_at(as_of)`.
  - `GET /identifiers/resolve` — `ref.identifier_at(as_of)` lookup.
  - `GET /financials/canonical` — `ts.canonical_financial_at(as_of)`,
    TAS 29 aware (returns both bases by default).
- **Response envelope (D15)** with `metadata.lineage` for every row,
  `metadata.feature_flags_active` snapshot, and `metadata.request_id`
  for tracing correlation.
- **Auth**: API key in `X-Aslan-Api-Key: <key_id>:<secret>` header;
  argon2id verification (provisional plain-text comparison ships
  in the alpha and is replaced in v1.0.0-beta).
- **Rate limits (D6)**: Postgres-backed token bucket, three tiers
  (`internal`, `partner`, `public`), per-key overrides via
  `aslan_core.api_key.rate_overrides`.
- **Audit log (D18)**: 13-month retention, every authenticated request
  inserts one row into `aslan_core.api_query_audit`.
- **OpenAPI 3.1 contract** at
  `docs/specs/bitemporal-research-api/OPENAPI.yaml`: 14 endpoint
  shapes, 28 component schemas, 9 reusable error responses.
- **Moat 2 canary** at `scripts/canary_moat_2.py` (skeleton; the
  `KNOWN_AMENDMENTS` set is populated in v1.0.0-beta).
- **Operational docs**: `README.md`, `RUNBOOK.md`, this `CHANGELOG.md`.
- **CI workflow**: `.github/workflows/bitemporal-api-ci.yml`
  (OpenAPI validation + ruff + mypy on the new files).

### Security

- API secrets stored as argon2id hash; never retrievable after
  creation (D5).
- PII fields redacted by default; `pii_unredacted=true` on
  `aslan_core.api_key` is a deliberate per-key opt-in (D30).
- All datetimes stored UTC; naive ISO8601 input rejected
  `400 BITEMPORAL_AS_OF_NAIVE` (D13).
- Audit log captures every authenticated request including PII access
  events at higher log priority.

### Known limitations

- All 14 endpoints implemented (rounds 2-6); cursor pagination
  emits `next_cursor` but the keyset-WHERE resumption is a v1.0.0-beta
  follow-up (TODO comments in place at every list endpoint).
- Argon2id auth integrated; the `verify_secret` helper retains a
  `hmac.compare_digest` fallback for non-`$argon2`-prefixed legacy
  hashes — to be removed after one release cycle.
- All 14 endpoints implemented including `/quality-scores` and
  `/openapi.json`. The OpenAPI 3.1 contract validates and is served
  at both `/v1/research/openapi.json` (custom route) and the
  FastAPI default `/openapi.json`.
- Phase 6 self-audit run: ruff + mypy --strict clean on every new
  file; reversibility cycle on shadow PASS (round 3).

---

## [v1.0.0-beta] — Planned

Target: Phase 6 self-audit complete and Phase 7 production-promotion
gate green. Internal partner-tier customers onboarded.

### Planned

- Argon2id verification in `research_auth.py::get_principal`
  (replacing the provisional plain-secret comparison).
- Remaining endpoints implemented end-to-end:
  `/financials/line-items`, `/entities`, `/entities/{id}`,
  `/filings`, `/events`, `/quality-scores`.
- `KNOWN_AMENDMENTS` populated with ≥10 hand-curated KAP-amendment
  cases; canary running every 5 minutes against staging.
- Python SDK at `aslan-core/sdk/python/` published to internal index.
- Grafana dashboards `bitemporal-api-overview`,
  `bitemporal-api-pit-latency`, `bitemporal-api-canary`,
  `bitemporal-api-audit-log` authored.
- Phase 6 self-audit: mypy strict, ruff, black, isort clean on the
  new files; reversibility cycle (`alembic downgrade base; alembic
  upgrade head`) idempotent on shadow.

---

## [v1.0.0] — Planned

Target: external-customer-ready release. Production master flag flipped
on; canary green ≥1h continuously; SLA documented.

### Planned

- `kap.disclosures` bitemporal upgrade (`CRAWL_PATCHES/0001`)
  applied via `crawl` PR; `BITEMPORAL_API_KAP_APPEND_ONLY_TRIGGER`
  flipped true; `/v1/research/disclosures*` endpoints fully live.
- `ref.entity` bitemporal upgrade (D10) — multi-week dedicated spec
  at `docs/specs/ref-entity-bitemporal-upgrade/`. Coordinated FK
  re-creation across `agg.*`, `doc.*`, `kap.*`, `ref.*`, `ts.*`.
- `/v1/research/disclosures` endpoint surfacing the full pre-bitemporal
  body of 462k disclosure rows under D3 NULL-as_of semantics.
- Replay endpoint `POST /v1/research/replay/<filing_event_id>` —
  re-extracts an `agg.filing_event` from raw bytes with the recorded
  `(prompt_version, model, seed)` and asserts schema-equality, per
  D29 replay tests. Requires `internal` tier.
- Cross-source joined views (currently out-of-scope per SCOPE.md §5).
  Likely v2 unless customer demand justifies v1.0.x.
- ADR-001/002/003 flipped from PROVISIONAL to FINAL after sidar review.
- Self-service API key portal (currently issued by email).

[v1.0.0-alpha]: https://github.com/kayadibi1/aslan-core/releases/tag/bitemporal-api-v1.0.0-alpha
[v1.0.0-beta]: #
[v1.0.0]: #
