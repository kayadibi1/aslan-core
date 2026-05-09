# Changelog — Bitemporal Research API

All notable changes to the Aslan Terminal Bitemporal Research API are
recorded here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning 2.0.0](https://semver.org/).

The deployed version is reported by `GET /v1/research/version` and as
`served_by: bitemporal-api/<commit-sha>` in every response envelope.

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
  - `0046_ref_entity_bitemporal_upgrade` — deferred (PK cascade across
    14+ FKs); reconstruction at API layer via `ref.entity_lineage`.
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

- `kap.disclosures` not yet bitemporal — schema is owned by `crawl`
  repo; awaiting `CRAWL_PATCHES/0001` PR. Endpoints
  `/v1/research/disclosures*` are stubbed in OpenAPI but return
  `503 FEATURE_DISABLED` until
  `BITEMPORAL_API_KAP_APPEND_ONLY_TRIGGER` is flipped.
- `ref.entity` upgrade deferred (D10); see
  [HANDOFF.md "What was deferred"](./HANDOFF.md#what-was-deferred-and-why).
- Argon2id verification not yet wired in the auth path; provisional
  plain-secret comparison ships in alpha (acceptable for internal-only
  release; replaced in v1.0.0-beta).
- Endpoints not yet implemented (skeleton in OpenAPI only):
  `/financials/line-items`, `/entities`, `/entities/{id}`,
  `/disclosures`, `/disclosures/{id}`, `/filings`, `/events`,
  `/quality-scores`, `/openapi.json`.
- Phase 6 self-audit (mypy strict, ruff, black, reversibility cycle)
  not yet run end-to-end.

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
