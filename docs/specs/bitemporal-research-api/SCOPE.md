# Bitemporal Research API — SCOPE

- **Status:** Binding scope; all 30 decisions resolved (D1–D30).
  Implementation spans Phases 2–7 of `bitemporal-api-prompt.md`.
  Decisions marked **PROVISIONAL** ship behind feature flags with safe
  defaults per H3.
- **Owner:** sidar
- **Drafted:** 2026-05-09 by agent during autonomous build per
  `bitemporal-api-prompt.md`.
- **Repo home:** `aslan-core/src/aslan_core/api/routes/research.py`
  plus migrations in `aslan-core/src/aslan_core/db/migrations/`,
  scripts in `aslan-core/scripts/`, SDK at `aslan-core/sdk/python/`.
- **Sibling repos:** `crawl` (kap.* schema owner — coordination
  required for kap.* trigger migrations); `aslan-event-extractor`
  (consumer; agg.* schema owner via aslan-core PR).
- **ADR dependencies:** ADR-001 (batch-first LLM), ADR-002 (language
  policy), ADR-003 (Bloomberg bar; Moat 2 = bitemporal point-in-time
  correctness). All three are PROVISIONAL stubs at draft time.

This service exposes the platform's `ts.*`, `ref.*`, `doc.*`,
`kap.*`, and `agg.*` data with full point-in-time semantics, such
that an authenticated client can ask "what did the platform know
on 2023-06-15 14:30:00 UTC" and receive the answer as recorded at
that time, not as it has since been revised. This is the
operational realization of **Moat 2** per ADR-003.

> **Scope discipline (per workspace `CLAUDE.md` §5):** This spec
> only covers the read API and its supporting bitemporal
> enforcement on existing tables. It does not invent new fact
> tables; new `agg.*` write paths belong in `aslan-event-extractor`.
> It does not change ingestion latency; that's `crawl` /
> `aslan-event-extractor` work. It does not localize Turkish text
> (per ADR-002).

---

## 0. Optimization criteria, in priority order

Every decision below is justified against:

1. **P1 — Bitemporal correctness.** The API's reason to exist.
   Append-only enforcement, immutable past, replayability, Moat 2
   canary green continuously. Non-negotiable.
2. **P2 — Sub-100ms p99 latency** on single-entity point-in-time
   queries on the production-shaped dataset. Realistic given
   Postgres `(entity_id, as_of)` indexing. (Per
   `bitemporal-api-prompt.md` Phase 4e target.)
3. **P3 — Auditability and observability.** Every request logged;
   every fact lineage-linked to `(source bytes, code version, prompt
   version, model identity, seed)`. Per ADR-003 replayability.
4. **P4 — Compliance.** GDPR (EU 2016/679) + KVKK (Law 6698) at
   schema level. PII handling, audit log, retention policy from M0
   (per `aslan-event-extractor/SCOPE.md` §6 GDPR).

---

## 1. State of the world (Hetzner prod, 2026-05-09)

Read this first; the API design is shaped by current data state.

| Fact | Source | Implication |
|---|---|---|
| `ref.identifier` already uses `tstzrange daterange` + GiST `EXCLUDE` | aslan-core/src/aslan_core/db/migrations/versions/20260427_1612_0002_ref_schema_and_tables.py | The PIT pattern for identifiers is established; reuse the same `tstzrange` discipline for any new daterange-style table. |
| `ts.observation` has 7.8M rows; uses `(series_id, ts, as_of)` | STATE.md §2 | Confirms the `(entity, time, as_of)` triple as the canonical pattern. PIT view via `DISTINCT ON (series_id, ts) ... ORDER BY series_id, ts, as_of DESC`. |
| `ts.financial_line_item` 7.3M rows, `ts.canonical_financial` 1.7M rows | STATE.md §2 | Targets for PIT exposure with TAS 29 restatement semantics (D8). |
| `kap.disclosures` 462,018 rows — 89,700 with `body_fetched=true` | STATE.md §2 | Pre-bitemporal mass: per D3, only ~89k carry a sensible `received_at`; the rest are NULL-as_of with documented semantics. |
| `kap.parsed_disclosures` 0 rows | STATE.md §2 | Tier 0 form parsing not yet enabled in prod (per `aslan-event-extractor/SCOPE.md` §1). PIT exposure of that table is built but returns empty until populated. |
| `doc.filing` 0 rows | STATE.md §2 | KAP writes only to `kap.*`. `aslan-event-extractor` M0 ships a backfill (`extract-migrate-doc-filing`). PIT exposure of `doc.filing` is built but returns empty until backfill runs. |
| `agg.filing_event` 0 rows | STATE.md §2 | M1 of `aslan-event-extractor` will populate. |
| `ref.entity` 775 rows | STATE.md §2 | Stable; PIT exposure is straightforward. |
| Live Postgres: container `aslan-dashboard-postgres-1`, db `aslan`, owner `aslan`, Postgres 16 + TimescaleDB | Phase-0 probe via `psql -l` | Migrations run as `aslan` role. TimescaleDB extensions present but not required for the bitemporal layer. |
| `aslan-dashboard-api-1` already runs FastAPI on `127.0.0.1:8600` with routes `/v1/auth`, `/v1/catalog`, `/v1/entities`, `/v1/financials` | aslan-core/src/aslan_core/api/routes/, STATE.md §2 | New router mounts at `/v1/research/` in the same process (D26, D27). |

---

## 2. Binding decisions

Each decision is recorded in `DECISIONS_LOG.json` with the same
fields. Reversibility is HIGH (≤ small code change, no data impact),
MEDIUM (data shape stable, but client/contract impact), or LOW
(schema or compliance impact). Per H3, every MEDIUM/LOW decision is
PROVISIONAL behind a feature flag with a safe default.

### D1 — Tables/views exposed

**Decision:** v1 exposes the following surfaces, each via a PIT
function and a thin REST layer:

| Surface | PIT function | API path |
|---|---|---|
| Time-series observations | `ts.observation_at(p_as_of timestamptz)` | `/v1/research/observations` |
| Financial line items (raw) | `ts.financial_line_item_at(p_as_of)` | `/v1/research/financials/line-items` |
| Canonical financials (TAS 29 aware) | `ts.canonical_financial_at(p_as_of)` | `/v1/research/financials/canonical` |
| Entities (registry) | `ref.entity_at(p_as_of)` | `/v1/research/entities` |
| Identifiers (namespace × value × daterange) | `ref.identifier_at(p_as_of)` | `/v1/research/identifiers/resolve` |
| KAP disclosures | `kap.disclosures_at(p_as_of)` | `/v1/research/disclosures` |
| Doc.filing | `doc.filing_at(p_as_of)` | `/v1/research/filings` |
| Filing events | `agg.filing_event_at(p_as_of)` | `/v1/research/events` |

**Rationale (P1, P3):** these are the platform's bitemporal-bearing
fact tables. Exposing them via SQL functions (rather than ad-hoc
joins in route handlers) keeps the bitemporal logic in one place,
testable in SQL, and reusable by the Python SDK and other internal
tools.

**Reversibility:** HIGH (function added/removed, no row impact).
**Status:** FINAL.
**Feature flag:** none beyond the master `BITEMPORAL_API_ENABLED`.
**Evidence:** STATE.md §2; existing aslan-core schemas.

### D2 — Query semantics

**Decision:** Three modes per endpoint:

1. **Point-in-time** — `?as_of=<ISO8601>`; default if omitted is
   `NOW()`.
2. **Interval** — `?as_of_range=[<T1>,<T2>)`; returns the version
   chain (every row whose `as_of` fell in the half-open interval).
3. **Current** — sugar for `?as_of=NOW()`; the default.

`as_of_range` returns at most `query_cost.max_versions_per_entity`
(default 50) versions per entity per page. If exceeded, response
includes `metadata.truncated=true` and clients paginate.

**Rationale (P1, P2):** Point-in-time and current cover 95% of
research workloads. Interval supports change-detection / amendment
analysis (the Moat 2 canary itself uses interval queries).

**Reversibility:** MEDIUM. Removing interval mode would break
clients that depend on amendment-chain queries.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_INTERVAL_QUERIES`. Default `true`
on staging, `true` on production once Moat 2 canary green ≥1h
(matches D27 master flag).
**Evidence:** Phase 4f of `bitemporal-api-prompt.md`.

### D3 — Pre-bitemporal `kap.disclosures` rows

**Decision:** For the 462k existing `kap.disclosures` rows:

- For rows where `body_fetched=true` (89.7k rows), populate
  `as_of` from `received_at` if column exists, else from
  `body_fetched_at`, else from `inserted_at`. Use the earliest
  available column with documented semantics
  ("`as_of_provenance`" enum: `received_at`, `body_fetched_at`,
  `inserted_at`, `pre_bitemporal_unknown`).
- For the remaining ~372k rows, `as_of` is **NULL** with
  `as_of_provenance='pre_bitemporal_unknown'`. Per ADR-002 we do
  not fabricate timestamps.
- API queries with explicit `?as_of=T` exclude
  `as_of IS NULL` rows by default (honest about uncertainty).
- API queries with `?include_pre_bitemporal=true` include them with
  a per-row `metadata.pre_bitemporal=true` marker; the response
  envelope flags `metadata.warnings` so clients know.

**Rationale (P1):** Better to surface NULL than invent. The Moat 2
contract says "what did the platform know at T?"; for these rows
we genuinely do not know — saying so is the correct answer.

**Reversibility:** LOW. Once the column is populated, undoing it
loses the provenance trail.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_ALLOW_NULL_AS_OF` (default `true`
on staging — needed for any data; required `true` on production
once D27 master flag enabled).
**Evidence:** STATE.md (462k disclosure rows); EXISTING_PATTERNS_AUDIT.md
column inventory.

### D4 — API surface

**Decision:** REST + OpenAPI 3.1. No GraphQL, no PostgREST, no
GRPC in v1. PostgREST was considered and rejected: the API has
non-trivial business logic (rate limits, audit log, feature flag
gating, pre-bitemporal handling) that PostgREST exposes awkwardly.

**Rationale (P3):** REST + OpenAPI gives auto-generated docs
(Swagger / ReDoc), simple SDK generation, and matches the existing
aslan-core API surface pattern.

**Reversibility:** HIGH (a v2 surface can sit alongside).
**Status:** FINAL.
**Feature flag:** none.
**Evidence:** existing `aslan-core/src/aslan_core/api/routes/`
patterns.

### D5 — Authentication

**Decision:** Long-lived API keys (UUID v4 + 32-byte random secret),
per-key rate limits (D6), audit logging on every authentication
event (D18), server-side hashing via `argon2id` for the secret.
Public verification endpoint (`/v1/verify/moat-2`) requires no auth.
JWT and OAuth deferred to v2.

Each API key carries:
- `key_id` (UUID, surface form for logging)
- `secret_hash` (argon2id of the secret; secret only shown at
  creation time, never retrievable)
- `rate_tier` (D6 enum)
- `pii_unredacted` (bool, D30)
- `created_at`, `expires_at` (default +1y), `revoked_at` (nullable)
- `description` (free text for ops)

**Rationale (P3):** API keys are simplest to implement and audit;
match the aslan-event-extractor compliance posture; defer JWT/OAuth
until enterprise customer demands it.

**Reversibility:** MEDIUM. Switching auth schemes would require key
rotation but data is unaffected.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_AUTH_ADDITIONAL_SCHEMES` (default
empty — only API keys until enabled).
**Evidence:** `aslan-event-extractor/SCOPE.md` §6 (audit log
requirements).

### D6 — Rate-limit tiers and quotas

**Decision:** Three tiers, enforced via Postgres-backed token bucket
in `aslan_core.api_rate_limit_state`:

| Tier | Per-minute | Per-hour | Per-day | Use |
|---|---|---|---|---|
| `internal` | 10000 | 100000 | unlimited | Aslan internal services, canary |
| `partner` | 600 | 5000 | 30000 | trusted external customers |
| `public` | 60 | 500 | 2000 | unauthenticated public endpoints (only `/verify/moat-2` in v1) |

Limits configurable per key in `aslan_core.api_key.rate_overrides
(jsonb)`.

**Rationale (P2):** sub-100ms p99 requires the rate-limiter itself
to be fast; Postgres + an LRU cache in front is acceptable until
RPS justifies Redis.

**Reversibility:** MEDIUM. Tier names baked into client expectations
once published.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_RATE_LIMIT_ENFORCED` (default
`true`; flipping `false` bypasses limits — use only for incident
response).
**Evidence:** `aslan-event-extractor/SCOPE.md` §6 (audit log table
shape mirrors).

### D7 — Pagination strategy

**Decision:** Cursor-based, opaque base64-encoded JSON cursor of
the form:

```json
{"v":1,
 "as_of":"<ISO8601>",
 "anchor":{"<entity_columns>":"...", "ts":"..."},
 "direction":"asc|desc",
 "filters_hash":"<sha256-of-applied-filters>"}
```

Default `limit=50`, max `limit=500`. Cursor is opaque to clients;
server validates the `filters_hash` to prevent cursor reuse across
different filter sets.

**Rationale (P2):** Cursor pagination is monotonic (no skip/limit
drift on inserts), safe under bitemporal append, and uses the
underlying `(entity, as_of)` index efficiently.

**Reversibility:** MEDIUM. Cursor format changes need a version
bump (handled via the `v` field).
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_CURSOR_VERSION` (default `1`;
allows in-place migration to v2).
**Evidence:** Postgres docs on keyset pagination; `ref.identifier`
PIT pattern.

### D8 — TAS 29 restated financials under `as_of`

**Decision:** A TAS 29 restatement of a previously-published
canonical financial line-item is recorded as a **new row** in
`ts.canonical_financial` with a later `as_of` and a
`restatement_kind='tas29'` tag column. PIT queries at the older
`as_of` return the nominal (un-restated) value; PIT queries at
the newer `as_of` return the restated value. Both rows persist
forever.

A `ts.canonical_financial.restatement_chain` materialized view
walks the chain per `(entity_id, period_end, line_item_id)` for
audit / replay.

**Rationale (P1, P3):** TAS 29 is workspace Moat 5 (per ADR-003);
its bitemporal correctness is a moat-of-a-moat. Restating in place
would break Moat 2.

**Reversibility:** LOW. Adds a column to `ts.canonical_financial`;
backfilling existing rows requires shadow-DB validation per H5.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_TAS29_RESTATEMENT_CHAIN` (default
`false` until Phase 2 column added; flip to `true` after Phase 2j
gate passes).
**Evidence:** workspace `CLAUDE.md` §4 Moat 5; aslan-core
`ts.canonical_financial` existing schema.

### D9 — Identifier change handling (ticker rename, VKN reassignment)

**Decision:** `ref.identifier` already uses `(namespace, value)
EXCLUDE USING gist (namespace WITH =, value WITH =, daterange
WITH &&)`. A ticker rename closes the old `daterange` and opens a
new one; both rows persist. The API endpoint
`/v1/research/identifiers/resolve` takes `(namespace, value, as_of)`
and returns the entity_id whose `daterange @> as_of`. If multiple
match (data bug), error `IDENTIFIER_AMBIGUOUS`.

**Rationale (P1):** Existing `ref.identifier` is already
bitemporal-correct on this; the API just exposes it. No new
discipline needed.

**Reversibility:** LOW. Schema is established; touching it would
break `ref.identifier`'s GiST EXCLUDE.
**Status:** FINAL (the underlying schema is established).
**Feature flag:** none.
**Evidence:** existing `ref.identifier` migration.

### D10 — Entity merge / split

**Decision:** A merge of entity B into entity A creates:

1. A new `ref.entity` row for A with later `as_of` and
   `merged_from_entity_ids` JSONB array including B.
2. A `ref.entity_lineage` row recording the merge event with
   `(merged_from, merged_into, as_of, reason)`.

PIT queries at `as_of < merge_time` see both A and B as separate
entities. PIT queries at `as_of >= merge_time` see only A with
the lineage pointer.

A split is the inverse: new entity rows for the split-off children
with `split_from_entity_id`.

**Rationale (P1):** Merges are real (TR market has had
reorganizations); a backtest at a pre-merge as_of should see the
pre-merge entity structure.

**Reversibility:** LOW. Adds columns to `ref.entity` and a new
table.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_ENTITY_MERGE_LINEAGE` (default
`false` until Phase 2 columns added).
**Evidence:** `ref.identifier` already does the daterange-based
version of this; `ref.entity` upgrade is the parallel.

### D11 — Idempotency on republish (KAP amendment)

**Decision:** A KAP-amended filing produces a new
`(disclosure_id, as_of)` row in `kap.disclosures`, never an UPDATE.
The `kap.disclosures.republished_as` column already points to the
amendment; the bitemporal layer records the amendment as a new row
with `as_of=republished_at`.

The append-only trigger on `kap.disclosures` (added in Phase 2)
enforces this at schema level. INSERTing the same `(disclosure_id,
as_of)` is a constraint violation; INSERTing a new `as_of` is
the only way to record an amendment.

**Rationale (P1):** Amendment chains are the load-bearing case for
Moat 2 — exactly what the canary tests.

**Reversibility:** LOW. Trigger + uniqueness change schema.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_KAP_APPEND_ONLY_TRIGGER` (default
`false` until Phase 2c migration applied; flip on after Phase 2j
gate passes).
**Evidence:** existing `kap.disclosures.republished_as_idx`
migration in aslan-core.

### D12 — `as_of` granularity

**Decision:** Microsecond — Postgres `timestamptz` native. No
truncation, no rounding. Two writes within the same microsecond
(rare) tie-break by `ctid` (Postgres row identifier); documented
behavior, never relied on by clients.

**Rationale (P1):** `timestamptz` is microsecond-precise. Going
finer would require a separate `as_of_seq bigint` column;
unwarranted complexity for our write rate.

**Reversibility:** HIGH. Storage type is established.
**Status:** FINAL.
**Feature flag:** none.
**Evidence:** Postgres `timestamptz` documentation.

### D13 — Timezone handling

**Decision:**
- Storage: UTC always. Naive datetimes raise (per
  `aslan-core/CLAUDE.md`).
- API request `?as_of=...`: ISO8601 with explicit `Z` or `±HH:MM`
  required; naive ISO8601 is rejected with HTTP 400 and error code
  `BITEMPORAL_AS_OF_NAIVE`.
- API response `as_of`: always UTC ISO8601 with `Z`.
- Optional: client passes `?display_tz=Europe/Istanbul` to receive
  a `local_time` companion field on every row; storage stays UTC,
  the local field is purely a convenience.

**Rationale (P1, P3):** UTC discipline is enforced upstream;
exposing naive datetimes at the API boundary would let clients
introduce ambiguity that bitemporal queries cannot recover from.

**Reversibility:** HIGH.
**Status:** FINAL.
**Feature flag:** none.
**Evidence:** aslan-core `CLAUDE.md` "All datetimes tz-aware (UTC).
Naive datetimes raise."

### D14 — NULL vs sentinel for unknown `as_of`

**Decision:** NULL for genuinely unknown. No sentinel
(no `1970-01-01`, no `infinity`). Per ADR-002, the platform does
not fabricate data. Pre-bitemporal rows surface via D3 only.

**Rationale (P1):** Sentinels propagate through aggregations and
sort orders silently; NULL is loud. The Moat 2 contract is
weakened by sentinel "as if known" timestamps.

**Reversibility:** MEDIUM. Adopting a sentinel later would require
a backfill.
**Status:** PROVISIONAL (paired with D3).
**Feature flag:** `BITEMPORAL_API_ALLOW_NULL_AS_OF` (same as D3).
**Evidence:** `bitemporal-api-prompt.md` D14 prompt.

### D15 — Response envelope

**Decision:**

```json
{
  "data": <object|array>,
  "metadata": {
    "as_of_requested": "<ISO8601 | null>",
    "as_of_resolved":  "<ISO8601>",
    "as_of_range":     ["<ISO8601>", "<ISO8601>"] | null,
    "lineage": {
      "source_filing_id": "<id|null>",
      "raw_bytes_sha256": "<hex|null>",
      "extraction_code_version": "<semver|null>",
      "prompt_version": "<id|null>",
      "model_identity": "<id|null>",
      "extraction_seed": "<int|null>"
    },
    "pagination": {
      "next_cursor": "<base64|null>",
      "prev_cursor": "<base64|null>",
      "has_more": false,
      "total_count_estimate": null
    },
    "feature_flags_active": ["BITEMPORAL_API_ENABLED", "..."],
    "warnings": [{"code": "...", "message": "...", "rows_affected": 0}],
    "request_id": "<uuid>",
    "served_by": "bitemporal-api/<commit-sha>"
  }
}
```

Lineage fields are populated where the underlying row carries them
(e.g., `agg.filing_event` always; `ts.observation` rarely).

**Rationale (P3):** Lineage in every response is the audit-trail
contract from ADR-003 replayability. Clients that don't care about
metadata simply ignore it.

**Reversibility:** MEDIUM. Adding fields is non-breaking (clients
ignore unknown). Removing fields breaks clients.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_ENVELOPE_VERSION` (default `1`).
**Evidence:** `aslan-event-extractor/SCOPE.md` §6 (audit log
shape).

### D16 — Error codes and envelope

**Decision:** RFC 7807 `application/problem+json`. Error envelope:

```json
{
  "type": "https://docs.aslanterminal.com/errors/<code>",
  "title": "<short>",
  "status": <http-status>,
  "detail": "<long>",
  "instance": "<request-uri>",
  "request_id": "<uuid>",
  "code": "<MACHINE_CODE>",
  "extensions": {<error-specific>}
}
```

Initial code set:
- `BITEMPORAL_AS_OF_NAIVE` (400) — datetime missing TZ
- `BITEMPORAL_AS_OF_FUTURE` (400) — `as_of > NOW() + 60s` (clock-skew tolerance)
- `BITEMPORAL_AS_OF_TOO_OLD` (400) — `as_of < platform_epoch`
- `BITEMPORAL_PRE_BITEMPORAL_REGION` (404) — entity has no
  bitemporal data at the requested `as_of`
- `BITEMPORAL_INTERVAL_INVALID` (400) — `T1 >= T2` or wrong shape
- `IDENTIFIER_AMBIGUOUS` (409) — multiple `ref.identifier` rows
  match
- `RATE_LIMITED` (429) — bucket exhausted; `Retry-After` header set
- `AUTH_INVALID` (401) — bad/missing/expired API key
- `AUTH_FORBIDDEN` (403) — key valid but lacks scope (e.g. PII)
- `QUERY_TOO_LARGE` (413) — exceeds D19 cost cap
- `FEATURE_DISABLED` (503) — required FF off (incl. master flag)
- `INTERNAL_ERROR` (500) — uncaught

**Rationale (P3):** RFC 7807 is the standard, machine-codes are
stable, `extensions` carries error-specific structure for debug.

**Reversibility:** MEDIUM. New codes additive; renaming breaks.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_ERROR_FORMAT_VERSION` (default `1`).
**Evidence:** RFC 7807; existing FastAPI conventions.

### D17 — Caching

**Decision:**
- For requests with `as_of <= NOW() - 5 minutes`: response is
  immutable. `Cache-Control: public, max-age=31536000, immutable`.
  ETag = sha256 of canonical response body.
- For requests with `as_of > NOW() - 5 minutes` (including default
  `NOW()`): response may change as new rows are appended.
  `Cache-Control: no-cache`. ETag set; clients can `If-None-Match`
  for cheap revalidation.
- `as_of_range` requests treated as immutable when `T2 <=
  NOW() - 5 minutes`, else `no-cache`.

The 5-minute window absorbs late-arriving extraction events
(Tier 2 verifier latency; per `aslan-event-extractor/SCOPE.md`
§7).

**Rationale (P2):** Immutable cache on past `as_of` is the largest
single performance lever. CDN-friendly.

**Reversibility:** HIGH. Cache headers are pure-output.
**Status:** FINAL.
**Feature flag:** none.
**Evidence:** RFC 7234; `aslan-event-extractor/SCOPE.md` Tier 2
latency.

### D18 — Per-query audit log

**Decision:** Every authenticated request inserts one row into
`aslan_core.api_query_audit`:

| column | type | notes |
|---|---|---|
| `audit_id` | uuid | pk |
| `requested_at` | timestamptz | not null, indexed |
| `api_key_id` | uuid | fk to `api_key`, nullable for `/verify/moat-2` |
| `request_ip` | inet | optional (consent) |
| `request_id` | uuid | tracing correlation |
| `endpoint` | text | e.g. `GET /v1/research/observations` |
| `query_params_sha256` | bytea | hash of canonical params (audit-friendly; raw params PII-risky) |
| `as_of_requested` | timestamptz | nullable (current=null) |
| `as_of_resolved` | timestamptz | not null |
| `rows_returned` | bigint | not null |
| `latency_ms` | int | not null |
| `status_code` | int | not null |
| `error_code` | text | nullable |
| `feature_flags_active` | text[] | snapshot |

Retention: 13 months (matches `aslan-event-extractor/SCOPE.md`
GDPR retention floor).

**Rationale (P3, P4):** Required for SOC II + GDPR + the
"who-asked-what-at-when" question. Hashing query params keeps the
log free of incidental PII.

**Reversibility:** LOW. Schema and retention policy are compliance
load-bearing.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_AUDIT_LOG_ENABLED` (default
`true`; flipping to `false` is for incident-response only and
itself logs to a higher-priority channel).
**Evidence:** `aslan-event-extractor/SCOPE.md` §6.

### D19 — Query cost limits

**Decision:** Per-request hard caps:

- `as_of_range` width ≤ 5 years (configurable per key).
- Page size ≤ 500 rows (default 50).
- Max distinct entity_ids in filter ≤ 100.
- Max nested filters / OR-clauses ≤ 10.
- Max query latency budget 30s (server-side `statement_timeout`
  applied per session).
- Max response body size 10 MiB.

Exceeded limits return `QUERY_TOO_LARGE` (413).

**Rationale (P2):** the API's p99 ≤100ms target is single-entity
PIT only; bulk queries pay for themselves via lower-priority queues
in future versions.

**Reversibility:** MEDIUM. Per-key overrides, so tuning is in DB.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_COST_LIMITS_ENFORCED` (default
`true`).
**Evidence:** Phase 4e load-test target.

### D20 — Moat 2 regression as public verification endpoint

**Decision:** `/v1/verify/moat-2` is unauthenticated, returns:

- `200` with `{"moat_2": "green", "last_run_at": "...", "cases_total": N, "cases_passing": N}` when canary is green.
- `503` with `{"moat_2": "red", "failing_cases": [...], "last_run_at": "..."}` when any case has failed in the last canary cycle.

Cached for 60s server-side; no per-IP rate limit (DDoS protected
by the upstream WAF).

**Rationale (P3, ADR-003):** The moat is verifiable by anyone, not
just internal monitoring. Customers / journalists / regulators can
hit this endpoint and audit our claim.

**Reversibility:** HIGH.
**Status:** FINAL.
**Feature flag:** none beyond master.
**Evidence:** ADR-003 §"Verification endpoint".

### D21 — Documentation surface

**Decision:** Swagger UI at `/v1/research/docs`, ReDoc at
`/v1/research/redoc`, raw OpenAPI 3.1 at
`/v1/research/openapi.json`. Customer-facing prose at
`docs/specs/bitemporal-research-api/README.md` (Phase 5b),
deployed alongside the API.

**Rationale (P3):** Standard FastAPI surfaces; Swagger for
exploration, ReDoc for static docs.

**Reversibility:** HIGH.
**Status:** FINAL.
**Feature flag:** none.
**Evidence:** FastAPI docs.

### D22 — SDK strategy

**Decision:** Python SDK (`aslan_research_sdk`) shipped at
`aslan-core/sdk/python/` in v1. Type-safe Pydantic v2 wrappers
around each endpoint, generated from OpenAPI but hand-tuned for
ergonomics. Other-language SDKs deferred per ADR-002.

The SDK includes:
- `Client(api_key, base_url)` constructor
- One method per endpoint with proper typing on `as_of: datetime`
- Pagination helper (`for row in client.observations.iter(...)`)
- Caching helper that respects HTTP cache headers

**Rationale (P3):** Python is the platform language (per ADR-002);
internal services consume the API via the SDK first; external
customers in v1 are mostly Python.

**Reversibility:** HIGH.
**Status:** FINAL.
**Feature flag:** none.
**Evidence:** ADR-002 SDK policy.

### D23 — URL versioning

**Decision:** `/v1/research/...`. Major-version bumps in path
(`/v2/...` is a parallel API surface; v1 stays live for
deprecation period). Minor / patch versions are
backward-compatible additions; the OpenAPI `info.version` reflects
the actual minor.

**Rationale (P3):** Path-based major versioning is the simplest
mental model for clients; matches existing `aslan-dashboard-api`
routes.

**Reversibility:** LOW. Clients pin to `/v1/`.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_V2_PREVIEW` (default `false`;
flipping `true` exposes `/v2/...` for preview).
**Evidence:** existing aslan-core API path layout.

### D24 — Deprecation policy

**Decision:** A deprecated endpoint or field carries:

1. `Deprecation: true` HTTP response header (per IETF draft).
2. `Sunset: <ISO8601>` HTTP response header indicating removal date.
3. OpenAPI `deprecated: true` and a `x-aslan-sunset` field with
   the same ISO8601.
4. CHANGELOG.md entry under "Deprecated" with the same date.
5. Minimum 6 months between deprecation announcement and removal.

**Rationale (P3):** Industry-standard headers, plus our own CHANGELOG
contract. Six months gives clients quarterly cadence to react.

**Reversibility:** HIGH.
**Status:** FINAL.
**Feature flag:** none.
**Evidence:** IETF Deprecation header draft; RFC 8594 (Sunset).

### D25 — Observability

**Decision:** Three pillars:

- **Metrics** (Prometheus, scraped on `:9001/metrics`):
  - `bitemporal_api_request_total{endpoint,status,api_key_id}`
  - `bitemporal_api_request_duration_seconds_bucket{endpoint,le}`
  - `bitemporal_api_audit_log_inserts_total`
  - `bitemporal_api_rate_limit_throttles_total{api_key_id}`
  - `bitemporal_api_pit_query_duration_seconds_bucket{table,le}`
  - `moat_2_canary_failures_total{case_id}`
  - `moat_2_canary_last_success_timestamp_seconds`
  - `bitemporal_api_feature_flag_state{flag,state}`
- **Tracing** (OTel; OTLP/gRPC to existing collector): every
  request traced; PIT SQL function calls are sub-spans; lineage
  fields attached as span attributes.
- **Logs** (structlog → stdout → existing log aggregation): JSON
  structured, `request_id` correlation across all events of a
  request.

**Rationale (P3):** Existing aslan-core observability stack.
Match it, don't reinvent it.

**Reversibility:** HIGH.
**Status:** FINAL.
**Feature flag:** none beyond master.
**Evidence:** existing `aslan_core.observability` package.

### D26 — Relationship to existing FastAPI surfaces

**Decision:** New router `aslan_core.api.routes.research`,
mounted at `/v1/research/`. Does **not** modify any existing route
(`/v1/auth`, `/v1/catalog`, `/v1/entities`, `/v1/financials`).
Reuses existing middleware (CORS, request-id) and adds:

- bitemporal-API auth dependency (`api_key` table; D5)
- per-route audit log dependency (D18)
- per-route rate-limit dependency (D6)
- per-route feature-flag gating dependency

The existing `entities` and `financials` routes are NOT
PIT-correct today; they are not modified by this work and remain
"current-only" surfaces. Migration to PIT for those routes is a
v2 follow-up.

**Rationale (P3):** Existing routes have other consumers (dashboard,
other clients); changing their semantics under feet is out of
scope. The bitemporal API is additive.

**Reversibility:** HIGH.
**Status:** FINAL.
**Feature flag:** none.
**Evidence:** existing `aslan-core/src/aslan_core/api/routes/`
inventory.

### D27 — Deployment shape

**Decision:** Same `aslan-dashboard-api-1` FastAPI process. The new
router is added behind the master flag `BITEMPORAL_API_ENABLED`
(default `false`; staging defaults `true` once Phase 5; production
stays `false` until Phase 7e). A separate service is **not** spun
up in v1; it would split observability and complicate deploys with
no commensurate benefit at v1 RPS.

If load eventually requires isolation (>500 RPS), v2 can split
without API surface change (clients still hit the same path; the
load balancer routes by path prefix).

**Rationale (P2, P3):** Single-process deploy is simplest;
Postgres is the bottleneck not the FastAPI layer at v1 RPS.

**Reversibility:** MEDIUM. Splitting later requires deploy/infra
work but no client-visible change.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_ENABLED` (master; default `false`
production, `false` staging until Phase 5g passes).
**Evidence:** STATE.md `aslan-dashboard-api-1` on
`127.0.0.1:8600`.

### D28 — Lockstep with future `as_of`-bearing tables

**Decision:** A new table
`aslan_core.bitemporal_table_registry` lists every
bitemporal-disciplined table with metadata:

```sql
CREATE TABLE aslan_core.bitemporal_table_registry (
  schema_name text NOT NULL,
  table_name text NOT NULL,
  entity_columns text[] NOT NULL,
  as_of_column text NOT NULL,
  pit_function_name text NOT NULL,
  api_path text NULL,                       -- null if not yet exposed
  exposed_in_api bool NOT NULL DEFAULT false,
  registered_at timestamptz NOT NULL DEFAULT now(),
  notes text NULL,
  PRIMARY KEY (schema_name, table_name)
);
```

Every alembic migration that adds a bitemporal table MUST add a
registry row. CI check
`scripts/check_bitemporal_table_registry.py` enforces:

1. Every table with an `as_of`-typed column has a registry row.
2. Every registry row points to an existing table and PIT function.
3. Every `exposed_in_api=true` row corresponds to an OpenAPI path.

The check runs on every push to any aslan-core branch and on every
migration application.

**Rationale (P1, P3):** Without this, bitemporal discipline drifts
silently as new tables ship.

**Reversibility:** LOW. The registry becomes the source of truth
for "what's bitemporal."
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_TABLE_REGISTRY_CI_ENFORCED` (default
`true` — CI fails on violation).
**Evidence:** `bitemporal-api-prompt.md` D28.

### D29 — Testing strategy

**Decision:**
- **Unit:** pytest, real Postgres via testcontainers. Each PIT
  function tested for: (1) empty table, (2) single-row entity,
  (3) multi-version entity, (4) before-first-version, (5)
  after-latest-version, (6) NULL `as_of` row exclusion (D3).
- **Trigger tests:** for every bitemporal table, attempt
  `UPDATE` and assert `feature_not_supported` exception fires
  (per H1, Phase 4a).
- **Constraint tests:** attempt INSERT of duplicate `(entity,
  as_of)` and assert constraint violation.
- **Integration:** testcontainers Postgres seeded with TESTPLAN.md
  fixtures; full HTTP request/response lifecycle.
- **Regression (Moat 2 canary):** ≥10 hand-curated historical
  amendments where `as_of_before` value ≠ `as_of_after` value;
  asserted continuously.
- **Fuzz:** Hypothesis on `as_of` parsing (random strings,
  edge timezones, leap seconds, year 9999), pagination
  cursor (random base64), auth header (random tokens).
- **Load:** locust, 100 RPS for 5 min, target p99 ≤100ms on
  single-entity PIT queries on a representative dataset.
- **Replay:** for ≥10 random `agg.filing_event` rows, re-run the
  extraction pipeline against the original raw bytes with the
  recorded `(prompt_version, model, seed)` and assert
  schema-equality of the extracted event.

**Rationale (P1):** Defense-in-depth; the schema-level append-only
contract is tested both via the trigger (positive) and via attempts
to bypass it (negative).

**Reversibility:** HIGH (test code).
**Status:** FINAL.
**Feature flag:** none.
**Evidence:** `bitemporal-api-prompt.md` Phase 4 sections.

### D30 — GDPR / KVKK posture

**Decision:**

- **Tables exposing PII** in v1: `kap.disclosures` (filer fields
  may include personal names of board signatories);
  `agg.filing_event` (counterparty names per
  `aslan-event-extractor/SCOPE.md` §5 `agg.filing_event_pii_index`).
- **Default API behavior:** PII fields **redacted** with
  `<REDACTED:reason>` regardless of `pii_unredacted` flag, except
  on API keys with `pii_unredacted=true` (D5).
- **Article 17 redaction registry** (per
  `aslan-event-extractor/SCOPE.md` D21): redaction events are
  themselves bitemporal — redacting a name produces a new `as_of`
  row, not an UPDATE. PIT queries before redaction `as_of` return
  the unredacted name (per Moat 2); PIT queries at or after return
  redacted. Audit log captures the access regardless.
- **Cross-border**: API responses with PII pass through the
  EU-region OpenAPI surface only; Hetzner is in Germany so this
  is the default. No PII to non-EU systems.
- **Audit log retention**: 13 months (D18).
- **PII access**: every PII-unredacted API call emits a higher-
  priority log line and increments
  `bitemporal_api_pii_access_total{key_id}`.

**Rationale (P4):** Compliance is schema-level, not retrofit;
matches `aslan-event-extractor/SCOPE.md` posture.

**Reversibility:** LOW. Compliance constraints don't reverse.
**Status:** PROVISIONAL.
**Feature flag:** `BITEMPORAL_API_PII_EXPOSURE` (default `false`;
must be deliberately enabled per environment).
**Evidence:** `aslan-event-extractor/SCOPE.md` §6 + D21.

---

## 3. Feature flag inventory (H3, H7)

| Flag | Default (staging) | Default (prod) | Tied to decision |
|---|---|---|---|
| `BITEMPORAL_API_ENABLED` (master) | `true` after Phase 5 gate | `false` until Phase 7e | D27 |
| `BITEMPORAL_API_INTERVAL_QUERIES` | `true` | `true` after canary green ≥1h | D2 |
| `BITEMPORAL_API_ALLOW_NULL_AS_OF` | `true` | `true` after canary green ≥1h | D3, D14 |
| `BITEMPORAL_API_AUTH_ADDITIONAL_SCHEMES` | empty | empty | D5 |
| `BITEMPORAL_API_RATE_LIMIT_ENFORCED` | `true` | `true` | D6 |
| `BITEMPORAL_API_CURSOR_VERSION` | `1` | `1` | D7 |
| `BITEMPORAL_API_TAS29_RESTATEMENT_CHAIN` | `true` after Phase 2 | `true` after Phase 7 | D8 |
| `BITEMPORAL_API_ENTITY_MERGE_LINEAGE` | `true` after Phase 2 | `true` after Phase 7 | D10 |
| `BITEMPORAL_API_KAP_APPEND_ONLY_TRIGGER` | `true` after Phase 2 | `true` after Phase 7 | D11 |
| `BITEMPORAL_API_ENVELOPE_VERSION` | `1` | `1` | D15 |
| `BITEMPORAL_API_ERROR_FORMAT_VERSION` | `1` | `1` | D16 |
| `BITEMPORAL_API_AUDIT_LOG_ENABLED` | `true` | `true` | D18 |
| `BITEMPORAL_API_COST_LIMITS_ENFORCED` | `true` | `true` | D19 |
| `BITEMPORAL_API_V2_PREVIEW` | `false` | `false` | D23 |
| `BITEMPORAL_TABLE_REGISTRY_CI_ENFORCED` | `true` | `true` | D28 |
| `BITEMPORAL_API_PII_EXPOSURE` | `false` | `false` | D30 |

Flags read from environment variables, with per-key overrides via
`aslan_core.feature_flags` table. The master flag
`BITEMPORAL_API_ENABLED` overrides every other; if it is `false`,
every endpoint returns `503 FEATURE_DISABLED` regardless of other
flags' state.

---

## 4. Schema impact summary (Phase 2 preview)

| Schema.Table | Change | Reversibility | Owner repo |
|---|---|---|---|
| `aslan_core.api_key` | new table | HIGH | aslan-core |
| `aslan_core.api_query_audit` | new table | HIGH | aslan-core |
| `aslan_core.api_rate_limit_state` | new table | HIGH | aslan-core |
| `aslan_core.feature_flags` | new table | HIGH | aslan-core |
| `aslan_core.bitemporal_table_registry` | new table | MEDIUM | aslan-core |
| `kap.disclosures` | add `as_of`, `as_of_provenance`, append-only trigger | LOW | crawl (PR coordination) |
| `kap.parsed_disclosures` | add `as_of`, append-only trigger | LOW | crawl (PR coordination) |
| `ts.observation` | append-only trigger only (`as_of` exists) | LOW | aslan-core |
| `ts.financial_line_item` | append-only trigger | LOW | aslan-core |
| `ts.canonical_financial` | add `restatement_kind`, append-only trigger | LOW | aslan-core |
| `ref.entity` | add `merged_from_entity_ids` jsonb, append-only trigger | LOW | aslan-core |
| `ref.entity_lineage` | new table | MEDIUM | aslan-core |
| `ref.identifier` | append-only trigger (already daterange-correct) | LOW | aslan-core |
| `doc.filing` | append-only trigger | LOW | aslan-core |
| `agg.filing_event` | append-only trigger (already as_of-bearing per `aslan-event-extractor/SCOPE.md`) | LOW | aslan-core |

PIT functions added per D1 (one per exposed table).

The crawl-side migrations are authored in this branch as patch
files under
`aslan-core/docs/specs/bitemporal-research-api/CRAWL_PATCHES/`
and surfaced in HANDOFF.md for crawl-side review. They are NOT
applied to crawl's alembic by this build (per
IMPLEMENTATION_NOTES.md).

---

## 5. Out of scope for v1

- Write APIs. The bitemporal API is read-only in v1.
- Cross-source joins via `ref.identifier` — supported via the
  `/identifiers/resolve` endpoint, but the API does not surface
  joined views (clients pull entities then resolve and pull facts
  separately). Joined views are v2 (D26).
- Delta / change-stream subscriptions. Customers ask "what
  changed since T?" via interval queries; a server-push
  subscription is v2.
- GraphQL (D4). PostgREST (D4). gRPC (D4).
- Automatic localization of Turkish content (per ADR-002).
- Materialized roll-ups (e.g. quarterly aggregates). Clients
  compute roll-ups themselves from PIT queries.

---

## 6. Acceptance / done definition for the spec

The bitemporal-research-API is "done" for v1 when:

1. Every fact table in §4 has an append-only trigger and unique
   constraint applied (Phase 2j gate).
2. `scripts/check_bitemporal_invariants.py` returns 0 violations
   on shadow and staging.
3. The Moat 2 canary has run ≥1 hour green continuously (Phase 4f).
4. Every endpoint in OPENAPI.yaml is implemented with all envelope
   fields populated (Phase 3 gate).
5. Python SDK passes its tests against the staging API (Phase 4).
6. Load test meets p99 ≤100ms target (Phase 4e).
7. Reversibility cycle clean (Phase 6c).
8. HANDOFF.md complete and draft PR open (Phase 6h).
9. PROMOTE_TO_PROD signal received and Phase 7 gate passes
   (production deploy; out-of-band sidar action).

Anything short of (1)-(8) means partial; (9) is sidar's call.
