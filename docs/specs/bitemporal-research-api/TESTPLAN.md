# Bitemporal Research API — TEST PLAN

- **Status:** Binding for the Phase 4 verification gate.
- **Owner:** sidar
- **Drafted:** 2026-05-09 by agent during autonomous build per
  `bitemporal-api-prompt.md`.
- **Companion docs:** `SCOPE.md` (D1–D30 binding decisions),
  `EXISTING_PATTERNS_AUDIT.md` (table inventory),
  `../../decisions/ADR-003-bloomberg-bar.md` (Moat 2 semantics),
  `IMPLEMENTATION_NOTES.md` (Phase ordering).

This file enumerates the verification surface for the Bitemporal
Research API. Every test case below is mapped to at least one
SCOPE.md decision (D1–D30) and, where relevant, to ADR-003 sections.
The plan is structured so that any one of pytest, the Moat 2 canary
runner, the alembic reversibility harness, the load-test harness,
and the OpenAPI conformance suite can pull cases by ID.

> **Implementation surfaces** referenced below:
>
> - **unit** — pytest, real Postgres via testcontainers, single
>   function or single endpoint under test (per D29).
> - **integration** — pytest, full HTTP request → FastAPI →
>   testcontainers Postgres lifecycle, with feature-flag /
>   audit-log / rate-limit dependencies wired.
> - **regression** — Moat 2 canary harness (per ADR-003 §"Moat 2
>   canary"); runs every 5 min in staging.
> - **fuzz** — Hypothesis-based property tests on parsing surfaces.
> - **load** — locust, 100 RPS for 5 min target per D29.
> - **canary** — public verification endpoint behavior tests
>   (`/v1/verify/moat-2`).
> - **db-invariant** — `scripts/check_bitemporal_invariants.py`
>   driven; verified per Phase 2j gate.
> - **replay** — `agg.filing_event` re-extraction harness from
>   recorded `(prompt_version, model, seed)` per D29.
> - **reversibility** — alembic up→down→up shadow-DB harness per H6.
> - **feature-flag** — flag matrix harness per H3 / H7 covering the
>   17 flags in SCOPE.md §3.

---

## Test inventory

| # | Category | Min cases (per brief) | Cases authored |
|---|---|---|---|
| 1 | Bitemporal correctness — point-in-time | ≥10 | 13 |
| 2 | Bitemporal correctness — interval | ≥5 | 6 |
| 3 | Append-only enforcement | ≥6 | 7 |
| 4 | Auth boundaries | ≥4 | 5 |
| 5 | Rate limits | ≥4 | 5 |
| 6 | Idempotency | ≥3 | 4 |
| 7 | Timezone correctness | ≥3 | 5 |
| 8 | Pagination | ≥3 | 4 |
| 9 | Query cost limits | ≥3 | 4 |
| 10 | Moat 2 canary regression | ≥3 | 4 |
| 11 | GDPR / KVKK | ≥3 | 4 |
| 12 | Caching | ≥2 | 3 |
| 13 | Schema-level invariants | ≥3 | 4 |
| 14 | Replayability | ≥2 | 2 |
| 15 | Reversibility | ≥2 | 2 |
| 16 | Feature flags | ≥3 | 4 |
| **Total** | | **≥50** | **74** |

### Count per implementation surface

| Surface | Cases |
|---|---|
| unit | 17 |
| integration | 30 |
| regression | 4 |
| fuzz | 3 |
| load | 2 |
| canary | 2 |
| db-invariant | 5 |
| replay | 2 |
| reversibility | 2 |
| feature-flag | 7 |

(A handful of cases declare two surfaces; the breakdown above is the
primary surface for each case.)

### Turkish-content coverage

- TC-007 — disclosure title `"Olağan Genel Kurul Toplantısı Sonucu"`
- TC-019 — disclosure title `"Sermaye Artırımı — Bedelli Pay Alma Hakkı"`
- TC-058 — counterparty name `"Şişe Cam Topluluğu A.Ş."`
- TC-067 — line-item label `"Hasılat — Yurt İçi Satışlar"`

(Per ADR-002 / Moat 4: Turkish strings preserved verbatim, never
auto-translated.)

---

## 1. Bitemporal correctness — point-in-time

### TC-001: Single-row entity, exact-match `as_of`

- **Category:** Bitemporal correctness — point-in-time
- **Traces to:** SCOPE.md D1, D2, D12; ADR-003 §"Schema discipline" (4)
- **Preconditions:**
  - One `ts.observation` row exists for `series_id='BIST.GARAN.close'`,
    `ts='2024-06-15T13:00:00Z'`, `as_of='2024-06-15T13:30:00Z'`,
    `value=42.10`.
- **Steps:**
  1. GET `/v1/research/observations?series_id=BIST.GARAN.close`
     `&ts=2024-06-15T13:00:00Z&as_of=2024-06-15T13:30:00Z`.
  2. Inspect `data` and `metadata.as_of_resolved`.
- **Expected:**
  - HTTP 200.
  - `data[0].value == 42.10`.
  - `metadata.as_of_resolved == "2024-06-15T13:30:00Z"`.
- **Pass criterion:** Returned value matches the seeded row exactly
  and `as_of_resolved` echoes the request.
- **Implementation:** unit

### TC-002: Multi-version entity, picks latest `as_of` ≤ requested

- **Category:** Bitemporal correctness — point-in-time
- **Traces to:** SCOPE.md D1, D2; ADR-003 §"Schema discipline" (4)
- **Preconditions:**
  - Three rows for `series_id='BIST.AKBNK.close'`,
    `ts='2024-08-01T13:00:00Z'`, with `as_of` =
    `2024-08-01T13:10:00Z` (`v1`),
    `2024-08-02T09:00:00Z` (`v2`),
    `2024-08-05T11:00:00Z` (`v3`).
- **Steps:**
  1. Query with `as_of=2024-08-03T00:00:00Z`.
  2. Inspect returned `value`.
- **Expected:**
  - Row corresponds to `v2` (latest as_of ≤ request).
- **Pass criterion:** Returned row's `as_of` is exactly the `v2`
  timestamp; later `v3` is invisible at this `as_of`.
- **Implementation:** unit

### TC-003: `as_of` strictly before any version returns empty

- **Category:** Bitemporal correctness — point-in-time
- **Traces to:** SCOPE.md D1, D16
- **Preconditions:**
  - Earliest `as_of` for entity `ASL-ENT-00000042` on
    `ts.financial_line_item` is `2023-04-01T08:00:00Z`.
- **Steps:**
  1. GET `/v1/research/financials/line-items?entity_id=ASL-ENT-00000042`
     `&as_of=2023-03-01T00:00:00Z`.
- **Expected:**
  - HTTP 200, `data == []`, `metadata.warnings` includes
    `BITEMPORAL_PRE_BITEMPORAL_REGION` advisory entry.
- **Pass criterion:** Empty array returned with the documented
  warning code; no row leaks from a later `as_of`.
- **Implementation:** unit

### TC-004: `as_of` after latest version returns latest

- **Category:** Bitemporal correctness — point-in-time
- **Traces to:** SCOPE.md D1
- **Preconditions:**
  - Latest `as_of` for `ts.canonical_financial` row
    `(ASL-ENT-00000042, 2023-12-31, "revenue")` is
    `2024-04-01T10:00:00Z`.
- **Steps:**
  1. Query with `as_of=2099-01-01T00:00:00Z` (well past last write).
- **Expected:**
  - HTTP 400 `BITEMPORAL_AS_OF_FUTURE` (per D16; clock-skew
    tolerance is 60s).
- **Pass criterion:** Far-future `as_of` is rejected with the
  documented error, not silently capped to NOW().
- **Implementation:** unit

### TC-005: Exact microsecond boundary returns the row at that `as_of`

- **Category:** Bitemporal correctness — point-in-time
- **Traces to:** SCOPE.md D12
- **Preconditions:**
  - Row recorded at `as_of='2024-09-10T15:23:11.654321Z'`.
- **Steps:**
  1. Query with `as_of=2024-09-10T15:23:11.654321Z`.
- **Expected:**
  - The row at that exact microsecond is returned.
- **Pass criterion:** Equality on `as_of` is inclusive (`as_of <=
  p_as_of`); the latest row at the exact boundary wins.
- **Implementation:** unit

### TC-006: Microsecond-edge boundary minus 1 µs returns prior row

- **Category:** Bitemporal correctness — point-in-time
- **Traces to:** SCOPE.md D12
- **Preconditions:**
  - Two rows for one entity; older `as_of=...654320Z`,
    newer `as_of=...654321Z`.
- **Steps:**
  1. Query `as_of=...654320Z`.
- **Expected:**
  - Older row returned; newer row invisible.
- **Pass criterion:** Microsecond comparison is strict; no rounding.
- **Implementation:** unit

### TC-007: NULL `as_of` excluded by default (D3)

- **Category:** Bitemporal correctness — point-in-time
- **Traces to:** SCOPE.md D3, D14
- **Preconditions:**
  - Disclosure row for `disclosure_id='kap-2018-44023'` with
    `title="Olağan Genel Kurul Toplantısı Sonucu"` and
    `as_of IS NULL`, `as_of_provenance='pre_bitemporal_unknown'`.
- **Steps:**
  1. GET `/v1/research/disclosures?as_of=2026-01-01T00:00:00Z`.
- **Expected:**
  - HTTP 200; `data` does not include `kap-2018-44023`.
  - `metadata.warnings` is empty (this is the default-honest path).
- **Pass criterion:** NULL-`as_of` rows do not leak into PIT
  responses without `include_pre_bitemporal=true`.
- **Implementation:** integration

### TC-008: Pre-bitemporal opt-in includes NULL `as_of` rows with marker

- **Category:** Bitemporal correctness — point-in-time
- **Traces to:** SCOPE.md D3, D14, D15
- **Preconditions:**
  - Same fixture as TC-007.
- **Steps:**
  1. GET same path with `&include_pre_bitemporal=true`.
- **Expected:**
  - `kap-2018-44023` appears in `data` with
    `pre_bitemporal=true`.
  - `metadata.warnings[0].code ==
    "BITEMPORAL_PRE_BITEMPORAL_INCLUDED"`.
- **Pass criterion:** Opt-in surfaces the row with explicit marker
  and warning, never silently.
- **Implementation:** integration

### TC-009: TAS 29 restatement chain — pre-restatement value at older `as_of`

- **Category:** Bitemporal correctness — point-in-time
- **Traces to:** SCOPE.md D8; ADR-003 Moat 5
- **Preconditions:**
  - `ts.canonical_financial` has two rows for
    `(ASL-ENT-00000042, 2022-12-31, "revenue")`:
    - `as_of='2023-03-15T09:00:00Z'`, `value=1000.00`,
      `restatement_kind=NULL`.
    - `as_of='2024-02-10T11:00:00Z'`, `value=1287.45`,
      `restatement_kind='tas29'`.
- **Steps:**
  1. Query at `as_of=2023-12-01T00:00:00Z`.
- **Expected:**
  - `value == 1000.00`, `restatement_kind` field is `null`.
- **Pass criterion:** Pre-restatement query returns nominal value;
  TAS 29 row is invisible at this as_of.
- **Implementation:** unit

### TC-010: TAS 29 restatement chain — restated value at newer `as_of`

- **Category:** Bitemporal correctness — point-in-time
- **Traces to:** SCOPE.md D8; ADR-003 Moat 5
- **Preconditions:** Same fixture as TC-009.
- **Steps:**
  1. Query at `as_of=2024-06-01T00:00:00Z`.
- **Expected:**
  - `value == 1287.45`, `restatement_kind == "tas29"`.
- **Pass criterion:** Restated row dominates at as_of past the
  restatement event; both rows persist forever in storage.
- **Implementation:** unit

### TC-011: TAS 29 restatement chain — materialized view walk

- **Category:** Bitemporal correctness — point-in-time
- **Traces to:** SCOPE.md D8
- **Preconditions:** Same fixture as TC-009; chain MV refreshed.
- **Steps:**
  1. Query the `ts.canonical_financial.restatement_chain` MV via
     internal endpoint `/v1/research/financials/canonical/chain`.
- **Expected:**
  - Chain returns 2 entries ordered by `as_of` ascending; the second
    is flagged `restatement_kind='tas29'`.
- **Pass criterion:** Chain walk exposes both rows in the order they
  were recorded; supports replay / audit.
- **Implementation:** integration

### TC-012: Identifier resolution at `as_of` before ticker rename

- **Category:** Bitemporal correctness — point-in-time
- **Traces to:** SCOPE.md D9
- **Preconditions:**
  - `ref.identifier` rows for `ASL-ENT-00000088`:
    - `(namespace='BIST', value='OLDC',
      daterange=[2018-01-01, 2022-04-30))`
    - `(namespace='BIST', value='NEWC',
      daterange=[2022-05-01, infinity))`
- **Steps:**
  1. GET `/v1/research/identifiers/resolve?namespace=BIST`
     `&value=OLDC&as_of=2021-06-01T00:00:00Z`.
- **Expected:**
  - HTTP 200; `entity_id == "ASL-ENT-00000088"`.
- **Pass criterion:** Old ticker resolves at pre-rename as_of; new
  ticker would not resolve at that as_of.
- **Implementation:** integration

### TC-013: Identifier resolution returns `IDENTIFIER_AMBIGUOUS` when daterange overlaps

- **Category:** Bitemporal correctness — point-in-time
- **Traces to:** SCOPE.md D9, D16
- **Preconditions:**
  - Test fixture deliberately seeds two `ref.identifier` rows whose
    `daterange` overlap (a data bug; the GiST EXCLUDE in production
    prevents this, but the API must defend against the case).
- **Steps:**
  1. Resolve at an `as_of` falling in the overlap.
- **Expected:**
  - HTTP 409 `IDENTIFIER_AMBIGUOUS`; both candidate `entity_id`s in
    `extensions.candidates`.
- **Pass criterion:** API never silently picks one of two
  ambiguous identifiers.
- **Implementation:** integration

---

## 2. Bitemporal correctness — interval

### TC-014: `as_of_range` returns the full version chain

- **Category:** Bitemporal correctness — interval
- **Traces to:** SCOPE.md D2
- **Preconditions:**
  - Three `as_of` versions for entity `ASL-ENT-00000042` on
    `ts.canonical_financial`, all within `[2024-01-01, 2024-04-01)`.
- **Steps:**
  1. GET `…?entity_id=ASL-ENT-00000042`
     `&as_of_range=[2024-01-01T00:00:00Z,2024-04-01T00:00:00Z)`.
- **Expected:**
  - 3 rows in `data`; `metadata.as_of_range` echoed; entries ordered
    by `as_of` ascending.
- **Pass criterion:** All versions whose `as_of` fell in the
  half-open interval are returned, none missed, none duplicated.
- **Implementation:** integration

### TC-015: Half-open vs closed bound semantics

- **Category:** Bitemporal correctness — interval
- **Traces to:** SCOPE.md D2
- **Preconditions:**
  - Versions at `as_of='2024-01-01T00:00:00Z'`,
    `as_of='2024-04-01T00:00:00Z'`.
- **Steps:**
  1. Query with `as_of_range=[2024-01-01T00:00:00Z,2024-04-01T00:00:00Z)`.
- **Expected:**
  - Returns the `2024-01-01` row only; the `2024-04-01` row is
    excluded by the open upper bound.
- **Pass criterion:** Half-open `[T1, T2)` is enforced literally;
  endpoint inclusion at `T1`, exclusion at `T2`.
- **Implementation:** unit

### TC-016: Empty `as_of_range` returns empty data

- **Category:** Bitemporal correctness — interval
- **Traces to:** SCOPE.md D2
- **Preconditions:**
  - Entity has versions only outside `[2025-01-01, 2025-02-01)`.
- **Steps:**
  1. Query with `as_of_range=[2025-01-01T00:00:00Z,2025-02-01T00:00:00Z)`.
- **Expected:**
  - `data == []`, no warning, HTTP 200.
- **Pass criterion:** Empty interval is a successful response, not
  an error; no rows leak from outside the bounds.
- **Implementation:** unit

### TC-017: `as_of_range` crossing the pre-bitemporal/bitemporal boundary

- **Category:** Bitemporal correctness — interval
- **Traces to:** SCOPE.md D2, D3
- **Preconditions:**
  - Disclosure has 1 NULL-`as_of` row and 2 bitemporal versions.
- **Steps:**
  1. Query `as_of_range=[2018-01-01T00:00:00Z,2025-01-01T00:00:00Z)`
     without `include_pre_bitemporal`.
  2. Repeat with `include_pre_bitemporal=true`.
- **Expected:**
  - Step 1: 2 rows.
  - Step 2: 3 rows; the pre-bitemporal one carries
    `pre_bitemporal=true` and a warning is set.
- **Pass criterion:** Range queries honor the same NULL-exclusion
  contract as point-in-time queries.
- **Implementation:** integration

### TC-018: `as_of_range` truncated when versions exceed cap

- **Category:** Bitemporal correctness — interval
- **Traces to:** SCOPE.md D2, D19
- **Preconditions:**
  - Single entity has 75 versions inside the requested range; default
    cap `query_cost.max_versions_per_entity=50`.
- **Steps:**
  1. Issue interval query with default page size.
- **Expected:**
  - `data` length == 50; `metadata.truncated == true`;
    `metadata.pagination.next_cursor` populated.
- **Pass criterion:** Cap is honored, response signals truncation,
  next page is reachable.
- **Implementation:** integration

### TC-019: `as_of_range` query, multiple amendments, Turkish title preserved

- **Category:** Bitemporal correctness — interval
- **Traces to:** SCOPE.md D2; ADR-002; ADR-003 Moat 4
- **Preconditions:**
  - `kap.disclosures` row `kap-2024-19044`,
    `title="Sermaye Artırımı — Bedelli Pay Alma Hakkı"`.
  - Two amendments (`as_of` advances on each).
- **Steps:**
  1. Issue interval query covering all three `as_of`.
- **Expected:**
  - 3 rows returned.
  - Every row's `title` is byte-equal to the seeded Turkish string
    (no normalization, no transliteration).
- **Pass criterion:** Turkish content is byte-preserved across the
  bitemporal layer; amendment chain is complete.
- **Implementation:** integration

---

## 3. Append-only enforcement

### TC-020: `UPDATE` on `ts.observation` raises `feature_not_supported`

- **Category:** Append-only enforcement
- **Traces to:** SCOPE.md D29; ADR-003 §"Schema discipline" (2);
  H1 (Phase 4a)
- **Preconditions:**
  - Phase 2 BEFORE UPDATE trigger applied to `ts.observation`.
  - Test has direct DB connection (not via the API).
- **Steps:**
  1. `UPDATE ts.observation SET value=99 WHERE series_id='BIST.GARAN.close' LIMIT 1`.
- **Expected:**
  - PostgreSQL exception `feature_not_supported` (SQLSTATE `0A000`)
    with the trigger message identifying the table.
- **Pass criterion:** Update raises; row is unchanged when read back.
- **Implementation:** unit (db-invariant)

### TC-021: `UPDATE` on `ts.financial_line_item` raises

- **Category:** Append-only enforcement
- **Traces to:** SCOPE.md D29; ADR-003
- **Preconditions:** Trigger applied.
- **Steps:**
  1. `UPDATE ts.financial_line_item SET value=0 WHERE entity_id='ASL-ENT-00000042'`.
- **Expected:** `feature_not_supported`.
- **Pass criterion:** Update raises; row contents byte-equal before
  vs after the failed attempt.
- **Implementation:** unit (db-invariant)

### TC-022: `UPDATE` on `ts.canonical_financial` raises

- **Category:** Append-only enforcement
- **Traces to:** SCOPE.md D8, D29
- **Preconditions:** Trigger applied.
- **Steps:**
  1. Attempt to mutate `value` in place.
  2. Attempt to mutate `restatement_kind` in place.
- **Expected:** Both raise.
- **Pass criterion:** TAS 29 restatement is achievable only via
  new-row insert, not via column mutation.
- **Implementation:** unit (db-invariant)

### TC-023: `UPDATE` on `agg.filing_event` raises (with `superseded_at` carve-out)

- **Category:** Append-only enforcement
- **Traces to:** SCOPE.md D29; EXISTING_PATTERNS_AUDIT.md
  `agg.filing_event` note
- **Preconditions:** Phase 2 trigger applied; per audit, the
  trigger is column-aware: it raises on every column except
  `superseded_at`, where the documented carve-out is permitted.
- **Steps:**
  1. Attempt `UPDATE … SET event_type='X'` → must raise.
  2. Attempt `UPDATE … SET superseded_at=now()` → permitted.
- **Expected:**
  - Step 1 raises `feature_not_supported`.
  - Step 2 succeeds.
- **Pass criterion:** Append-only is enforced on every fact column;
  the documented `superseded_at` carve-out is the only exception.
- **Implementation:** unit (db-invariant)

### TC-024: `UPDATE` on `ref.entity` raises after Phase 2

- **Category:** Append-only enforcement
- **Traces to:** SCOPE.md D10, D29
- **Preconditions:**
  - Phase 2 has added `as_of`, `merged_from_entity_ids`, and
    BEFORE UPDATE trigger to `ref.entity`.
- **Steps:**
  1. `UPDATE ref.entity SET name='X' WHERE entity_id='ASL-ENT-00000042'`.
- **Expected:** `feature_not_supported`.
- **Pass criterion:** Existing 775-row table is now append-only;
  rename / merge produces new rows.
- **Implementation:** unit (db-invariant)

### TC-025: `UPDATE` on `kap.disclosures` raises after Phase 2

- **Category:** Append-only enforcement
- **Traces to:** SCOPE.md D11, D29; EXISTING_PATTERNS_AUDIT.md
  §"462k pre-bitemporal kap.disclosures problem"
- **Preconditions:**
  - Phase 2 has converted the `body_fetched=true` UPDATE to an
    insert pattern AND added a BEFORE UPDATE trigger.
- **Steps:**
  1. `UPDATE kap.disclosures SET body_fetched=true …`.
- **Expected:** `feature_not_supported`; the legacy crawl mutation
  path is provably gone.
- **Pass criterion:** Updates on `kap.disclosures` are rejected
  schema-side; body fetch state is recorded as new rows.
- **Implementation:** unit (db-invariant)

### TC-026: Duplicate `(entity, as_of)` insert on Class A table fails

- **Category:** Append-only enforcement
- **Traces to:** SCOPE.md D11, D29; ADR-003 §"Schema discipline" (3)
- **Preconditions:**
  - Row exists for
    `(series_id='BIST.GARAN.close', ts='2024-06-15T13:00:00Z',
    as_of='2024-06-15T13:30:00Z')`.
- **Steps:**
  1. Re-insert the exact same `(series_id, ts, as_of)` triple with
     a different `value`.
- **Expected:**
  - Unique-constraint violation; original row preserved.
- **Pass criterion:** Same `(entity, as_of)` collision is impossible
  by construction — the only way to record a revision is a new
  `as_of`.
- **Implementation:** unit (db-invariant)

---

## 4. Auth boundaries

### TC-027: Missing API key → 401 `AUTH_INVALID`

- **Category:** Auth boundaries
- **Traces to:** SCOPE.md D5, D16
- **Preconditions:**
  - API server running with auth dependency wired.
- **Steps:**
  1. GET `/v1/research/observations?series_id=BIST.GARAN.close`
     with no `Authorization` header.
- **Expected:**
  - HTTP 401, `application/problem+json`, `code='AUTH_INVALID'`,
    `request_id` populated.
- **Pass criterion:** Anonymous requests never reach the data layer.
- **Implementation:** integration

### TC-028: Invalid API key → 401 `AUTH_INVALID`

- **Category:** Auth boundaries
- **Traces to:** SCOPE.md D5, D16
- **Preconditions:** Server up; no key with the supplied UUID exists.
- **Steps:**
  1. GET with `Authorization: Bearer aslan_test_<random>`.
- **Expected:**
  - HTTP 401 `AUTH_INVALID`.
  - Audit log row written with `error_code='AUTH_INVALID'`,
    `api_key_id IS NULL`.
- **Pass criterion:** Invalid key fails closed and is logged.
- **Implementation:** integration

### TC-029: Expired API key → 401 `AUTH_INVALID`

- **Category:** Auth boundaries
- **Traces to:** SCOPE.md D5
- **Preconditions:**
  - Key seeded with `expires_at = NOW() - 1 day`.
- **Steps:**
  1. GET with that key.
- **Expected:**
  - HTTP 401 `AUTH_INVALID`; `extensions.reason='EXPIRED'`.
- **Pass criterion:** Expiry is enforced server-side, not just at
  rotation time; the audit log captures the reason.
- **Implementation:** integration

### TC-030: Valid key without `pii_unredacted` scope on PII endpoint

- **Category:** Auth boundaries
- **Traces to:** SCOPE.md D5, D30
- **Preconditions:**
  - Key has `pii_unredacted=false`.
  - `agg.filing_event_pii_index` row exists for entity
    `ASL-ENT-00000042`.
- **Steps:**
  1. GET `/v1/research/events?entity_id=ASL-ENT-00000042`
     `&fields=counterparty_name`.
- **Expected:**
  - HTTP 200, but `counterparty_name == "<REDACTED:pii>"` per D30.
  - No 403 — redaction is the default; 403 is reserved for endpoints
    that require the scope to even reach.
- **Pass criterion:** Redaction is the default behavior; the scope
  is required to *opt out*, not to *opt in*.
- **Implementation:** integration

### TC-031: Valid key with `pii_unredacted=true` returns unredacted

- **Category:** Auth boundaries
- **Traces to:** SCOPE.md D5, D30
- **Preconditions:**
  - Key has `pii_unredacted=true`.
  - Same fixture as TC-030.
- **Steps:**
  1. Same GET.
  2. Inspect audit log + `bitemporal_api_pii_access_total`.
- **Expected:**
  - HTTP 200; `counterparty_name` populated with the real value.
  - Audit row written; PII counter incremented by 1.
  - Higher-priority log line emitted.
- **Pass criterion:** Unredacted access is allowed AND observable;
  the audit trail captures the access for KVKK / GDPR Article 30.
- **Implementation:** integration

---

## 5. Rate limits

### TC-032: Within limit — 200 OK

- **Category:** Rate limits
- **Traces to:** SCOPE.md D6
- **Preconditions:**
  - `partner` tier key (600 RPM); test issues 5 RPS for 10s.
- **Steps:**
  1. Burst 50 GETs.
- **Expected:**
  - All return HTTP 200.
- **Pass criterion:** Bucket holds; `429` not emitted; latency p99
  ≤ 100ms (per D29 / Moat 2 latency target).
- **Implementation:** integration

### TC-033: At limit — last request inside window succeeds

- **Category:** Rate limits
- **Traces to:** SCOPE.md D6
- **Preconditions:**
  - `partner` tier; issue exactly 600 requests in 60s with
    consistent pacing.
- **Steps:**
  1. Issue requests timed to land inside one minute window.
- **Expected:**
  - All 600 → 200.
  - 601st in same window → 429.
- **Pass criterion:** Boundary is exactly the documented per-tier
  quota; no off-by-one.
- **Implementation:** integration

### TC-034: Over limit → 429 `RATE_LIMITED` with `Retry-After`

- **Category:** Rate limits
- **Traces to:** SCOPE.md D6, D16
- **Preconditions:** As above; immediately issue 700 requests.
- **Steps:**
  1. Send 700 GETs.
- **Expected:**
  - First 600 → 200; remaining → 429 with
    `code='RATE_LIMITED'`, `Retry-After` header populated with
    seconds until window reset, `extensions.bucket='per-minute'`.
- **Pass criterion:** Throttled responses carry actionable
  `Retry-After`; the rate-limit metric is incremented per throttle.
- **Implementation:** integration

### TC-035: Recovery after window — bucket refills

- **Category:** Rate limits
- **Traces to:** SCOPE.md D6
- **Preconditions:** TC-034 has just run.
- **Steps:**
  1. Wait `Retry-After` seconds.
  2. Send a single GET.
- **Expected:**
  - HTTP 200; bucket has refilled to ≥1.
- **Pass criterion:** Token bucket refill is real wall-clock based,
  not session-pinned.
- **Implementation:** integration

### TC-036: Rate limit bypass when flag off (incident-response posture)

- **Category:** Rate limits
- **Traces to:** SCOPE.md D6 (feature flag
  `BITEMPORAL_API_RATE_LIMIT_ENFORCED`)
- **Preconditions:**
  - Flag `BITEMPORAL_API_RATE_LIMIT_ENFORCED=false` in the test env.
- **Steps:**
  1. Same flood as TC-034.
- **Expected:**
  - Every request returns 200; counter
    `bitemporal_api_rate_limit_throttles_total` does not increment.
  - `metadata.feature_flags_active` includes the bypass flag state.
- **Pass criterion:** Documented incident-response bypass works AND
  is visible in responses.
- **Implementation:** feature-flag

---

## 6. Idempotency

### TC-037: KAP amendment produces new `as_of` row

- **Category:** Idempotency
- **Traces to:** SCOPE.md D11; ADR-003 Moat 2
- **Preconditions:**
  - Existing disclosure `kap-2024-19044` with
    `as_of='2024-08-01T10:00:00Z'`.
  - Amendment fixture: same `disclosure_id`, new content,
    `as_of='2024-08-02T13:00:00Z'`.
- **Steps:**
  1. Apply the amendment to the database via the producer interface.
  2. GET `/v1/research/disclosures?disclosure_id=kap-2024-19044`
     `&as_of_range=[2024-07-30T00:00:00Z,2024-08-05T00:00:00Z)`.
- **Expected:**
  - Two rows returned, distinct `as_of`, same `disclosure_id`,
    differing fields where the amendment changed them.
- **Pass criterion:** Amendments are recorded as new rows; no
  `UPDATE` ever runs on `kap.disclosures` after Phase 2.
- **Implementation:** integration

### TC-038: Duplicate `(disclosure_id, as_of)` blocked

- **Category:** Idempotency
- **Traces to:** SCOPE.md D11
- **Preconditions:**
  - Row already exists for `(kap-2024-19044, 2024-08-02T13:00:00Z)`.
- **Steps:**
  1. Re-insert the identical pair.
- **Expected:** Unique constraint violation.
- **Pass criterion:** Producer code that re-fires the same amendment
  does not silently overwrite — it must advance `as_of`.
- **Implementation:** unit

### TC-039: Same request → same `served_by` and same response body

- **Category:** Idempotency
- **Traces to:** SCOPE.md D15
- **Preconditions:**
  - Server fixed to commit `8f3a1c2`. Past-as_of immutable region.
- **Steps:**
  1. Issue GET twice with identical params at past-as_of.
  2. Compare `metadata.served_by` and the canonical body.
- **Expected:**
  - Both responses have `served_by="bitemporal-api/8f3a1c2"`.
  - Canonical body is byte-equal.
- **Pass criterion:** Past-as_of replay is deterministic for a fixed
  code version; supports the cache-immutable contract (D17).
- **Implementation:** integration

### TC-040: Replay of audit-log query returns same canonical body

- **Category:** Idempotency
- **Traces to:** SCOPE.md D15, D18
- **Preconditions:**
  - Audit log has an entry from a past run with
    `query_params_sha256` recorded.
- **Steps:**
  1. Reconstruct the request from the log row.
  2. Replay it.
- **Expected:**
  - `query_params_sha256` of the replay matches the logged one.
  - Response is byte-equal to the original (modulo `request_id`).
- **Pass criterion:** Audit-log replay is faithful; supports
  forensic and compliance reconstruction.
- **Implementation:** integration

---

## 7. Timezone correctness

### TC-041: Naive datetime rejected with `BITEMPORAL_AS_OF_NAIVE`

- **Category:** Timezone correctness
- **Traces to:** SCOPE.md D13, D16; aslan-core CLAUDE.md "Naive
  datetimes raise"
- **Steps:**
  1. GET `…?as_of=2024-06-15T13:30:00` (no Z, no offset).
- **Expected:**
  - HTTP 400 `BITEMPORAL_AS_OF_NAIVE`.
- **Pass criterion:** No coercion to UTC; error is loud and the
  audit row records the rejection.
- **Implementation:** unit

### TC-042: UTC `Z` suffix accepted

- **Category:** Timezone correctness
- **Traces to:** SCOPE.md D13
- **Steps:**
  1. GET with `as_of=2024-06-15T13:30:00Z`.
- **Expected:**
  - HTTP 200; `metadata.as_of_resolved` is byte-equal echo.
- **Pass criterion:** UTC-Z is the canonical accepted form.
- **Implementation:** unit

### TC-043: Negative-offset ISO8601 accepted, normalized to UTC

- **Category:** Timezone correctness
- **Traces to:** SCOPE.md D13
- **Steps:**
  1. GET with `as_of=2024-06-15T09:30:00-04:00`.
- **Expected:**
  - HTTP 200; `metadata.as_of_resolved == "2024-06-15T13:30:00Z"`.
- **Pass criterion:** Offset is normalized to UTC for storage and
  echo; precision preserved (microseconds untouched).
- **Implementation:** unit

### TC-044: `display_tz=Europe/Istanbul` adds `local_time` companion

- **Category:** Timezone correctness
- **Traces to:** SCOPE.md D13
- **Preconditions:** Row stored at `ts='2024-06-15T13:30:00Z'`.
- **Steps:**
  1. GET with `&display_tz=Europe/Istanbul`.
- **Expected:**
  - Each row carries `local_time='2024-06-15T16:30:00+03:00'`
    (Istanbul is UTC+03:00 year-round, no DST since 2016).
  - Storage `ts` field unchanged (still UTC).
- **Pass criterion:** `local_time` is purely additive; underlying
  data is not converted.
- **Implementation:** integration

### TC-045: Invalid IANA `display_tz` rejected

- **Category:** Timezone correctness
- **Traces to:** SCOPE.md D13, D16
- **Steps:**
  1. GET with `&display_tz=Europe/NotARealCity`.
- **Expected:**
  - HTTP 400 with a validation error referencing IANA TZ database.
- **Pass criterion:** Invalid TZs do not silently fall back to UTC.
- **Implementation:** fuzz

---

## 8. Pagination

### TC-046: First-page query returns `next_cursor`

- **Category:** Pagination
- **Traces to:** SCOPE.md D7
- **Preconditions:** ≥75 versions exist for an entity in range.
- **Steps:**
  1. GET with default `limit=50`.
- **Expected:**
  - `data.length == 50`;
    `metadata.pagination.has_more == true`;
    `metadata.pagination.next_cursor` is a base64 JSON cursor whose
    decoded `v=1`.
- **Pass criterion:** Pagination envelope is populated correctly.
- **Implementation:** integration

### TC-047: Following `next_cursor` returns the next contiguous page

- **Category:** Pagination
- **Traces to:** SCOPE.md D7
- **Preconditions:** TC-046 just ran.
- **Steps:**
  1. GET `…?cursor=<next_cursor>`.
- **Expected:**
  - `data` is the next 25 rows; cursor anchor advances; no row
    overlaps with page 1.
- **Pass criterion:** Cursor pagination is monotonic and gap-free.
- **Implementation:** integration

### TC-048: Cursor reuse with mismatched filters rejected

- **Category:** Pagination
- **Traces to:** SCOPE.md D7
- **Preconditions:** Cursor obtained from TC-046 with
  `series_id=BIST.GARAN.close`.
- **Steps:**
  1. GET `?series_id=BIST.AKBNK.close&cursor=<old_cursor>`.
- **Expected:**
  - HTTP 400 with code `PAGINATION_CURSOR_MISMATCH`;
    `extensions.expected_filters_hash` and `received_filters_hash`
    set.
- **Pass criterion:** `filters_hash` validation prevents cursor
  splicing across distinct queries.
- **Implementation:** integration

### TC-049: Cursor with unknown `v` rejected

- **Category:** Pagination
- **Traces to:** SCOPE.md D7
- **Steps:**
  1. Construct cursor with `{"v": 99, …}` and submit.
- **Expected:**
  - HTTP 400; error references unsupported cursor version.
- **Pass criterion:** Forward-compat versioning works; future v2
  cursor format will not be silently accepted today.
- **Implementation:** unit

---

## 9. Query cost limits

### TC-050: `as_of_range` width > 5 years rejected

- **Category:** Query cost limits
- **Traces to:** SCOPE.md D19
- **Steps:**
  1. GET with `as_of_range=[2018-01-01T00:00:00Z,2024-06-01T00:00:00Z)`
     (~6.4 years).
- **Expected:**
  - HTTP 413 `QUERY_TOO_LARGE`;
    `extensions.limit='as_of_range_max_years'`,
    `extensions.value=5`.
- **Pass criterion:** Cap is enforced at the route level before any
  SQL runs.
- **Implementation:** unit

### TC-051: Page size > 500 rejected

- **Category:** Query cost limits
- **Traces to:** SCOPE.md D7, D19
- **Steps:**
  1. GET with `limit=600`.
- **Expected:**
  - HTTP 413 `QUERY_TOO_LARGE`; `extensions.limit='page_size_max'`.
- **Pass criterion:** No quiet cap to 500; the client gets a clear
  error.
- **Implementation:** unit

### TC-052: > 100 distinct entity_ids in filter rejected

- **Category:** Query cost limits
- **Traces to:** SCOPE.md D19
- **Steps:**
  1. GET with `entity_id=ASL-ENT-00000001`,
     `entity_id=ASL-ENT-00000002`, … 101 entries.
- **Expected:**
  - HTTP 413; `extensions.limit='entity_ids_max'`.
- **Pass criterion:** Bulk-fanout is bounded; clients use pagination
  or per-entity loops.
- **Implementation:** unit

### TC-053: 30s server-side `statement_timeout` enforced

- **Category:** Query cost limits
- **Traces to:** SCOPE.md D19
- **Preconditions:**
  - Test harness can issue an artificially expensive query (e.g.,
    no index path) by removing test indices in a fixture DB.
- **Steps:**
  1. Issue the expensive query.
- **Expected:**
  - PostgreSQL raises `query_canceled` (SQLSTATE `57014`); FastAPI
    converts to HTTP 504 `QUERY_TIMEOUT` (mapped from `INTERNAL_ERROR`
    family but with explicit code per implementation).
- **Pass criterion:** No request can hold a connection longer than
  the documented budget.
- **Implementation:** load

---

## 10. Moat 2 canary regression

### TC-054: Canary happy path — known amendment, both points correct

- **Category:** Moat 2 canary regression
- **Traces to:** SCOPE.md D20; ADR-003 §"Moat 2 canary"
- **Preconditions:**
  - Curated `KNOWN_AMENDMENTS` list contains
    `{"filing_id": "kap-2024-19044",
      "as_of_before": "2024-08-01T11:00:00Z",
      "as_of_after":  "2024-08-02T14:00:00Z",
      "field": "dividend_amount",
      "expected_before": 1.50,
      "expected_after":  1.75}`.
- **Steps:**
  1. `python scripts/canary_moat_2.py --once`.
  2. Inspect canary report.
- **Expected:**
  - Case passes; report records both lookups returned the expected
    values.
  - `bitemporal_api_pit_query_duration_seconds` p99 ≤ 100ms.
- **Pass criterion:** Curated amendment chain reproduces exactly via
  the API; the canary marks green.
- **Implementation:** regression

### TC-055: Canary detects regression — corrupt the bitemporal layer

- **Category:** Moat 2 canary regression
- **Traces to:** SCOPE.md D20; ADR-003 §"Moat 2 canary"
- **Preconditions:**
  - Test harness flips a fixture row's `value` directly via a SQL
    that bypasses the trigger (the harness uses
    `SET session_replication_role = 'replica';` to disable the
    trigger for the test only).
- **Steps:**
  1. Run canary.
- **Expected:**
  - Case fails; `failing_cases` contains the corrupted entry;
    canary exits non-zero.
- **Pass criterion:** A real regression is caught — proves the
  canary is not a no-op.
- **Implementation:** regression

### TC-056: `/v1/verify/moat-2` returns `green` shape when canary green

- **Category:** Moat 2 canary regression
- **Traces to:** SCOPE.md D20
- **Preconditions:**
  - All canary cases passed in the last 5-min cycle.
- **Steps:**
  1. GET `/v1/verify/moat-2` (no auth).
- **Expected:**
  - HTTP 200; body `{"moat_2": "green", "last_run_at": "...",
    "cases_total": N, "cases_passing": N}`;
    `Cache-Control: public, max-age=60`.
- **Pass criterion:** Public verification endpoint exposes truthful
  green state with the documented shape.
- **Implementation:** canary

### TC-057: `/v1/verify/moat-2` returns `red` shape when canary red

- **Category:** Moat 2 canary regression
- **Traces to:** SCOPE.md D20; ADR-003 §"Verification endpoint"
- **Preconditions:** Canary fixture corrupted as in TC-055.
- **Steps:**
  1. GET `/v1/verify/moat-2`.
- **Expected:**
  - HTTP 503; body `{"moat_2": "red",
    "failing_cases": [{"case_id": "...", "filing_id": "...",
    "expected_before": ..., "actual_before": ...}],
    "last_run_at": "..."}`.
- **Pass criterion:** Truthful red state is exposed publicly with
  enough structure to debug.
- **Implementation:** canary

---

## 11. GDPR / KVKK

### TC-058: PII redacted by default — Turkish counterparty name

- **Category:** GDPR / KVKK
- **Traces to:** SCOPE.md D30
- **Preconditions:**
  - `agg.filing_event` row references counterparty
    `"Şişe Cam Topluluğu A.Ş."` indexed in
    `agg.filing_event_pii_index`.
  - API key has `pii_unredacted=false`.
- **Steps:**
  1. GET event with `fields=counterparty_name`.
- **Expected:**
  - `counterparty_name == "<REDACTED:pii>"`.
  - Underlying Turkish content preserved verbatim in storage —
    verified via direct DB read in the same test.
- **Pass criterion:** Redaction at the API surface; storage byte
  preservation honors Moat 4.
- **Implementation:** integration

### TC-059: `pii_unredacted=true` returns unredacted with audit + counter

- **Category:** GDPR / KVKK
- **Traces to:** SCOPE.md D30
- **Preconditions:** Same fixture as TC-058 with key flipped.
- **Steps:**
  1. GET same path.
- **Expected:**
  - `counterparty_name` matches the seeded Turkish string byte-for-byte.
  - Audit log row written with `feature_flags_active` including
    `BITEMPORAL_API_PII_EXPOSURE`.
  - `bitemporal_api_pii_access_total{key_id}` increments by 1.
  - Higher-priority log line emitted at `WARNING`.
- **Pass criterion:** Unredacted access is observable end-to-end.
- **Implementation:** integration

### TC-060: Redaction event itself bitemporal — pre-redaction `as_of`

- **Category:** GDPR / KVKK
- **Traces to:** SCOPE.md D30 (Article 17 redaction registry)
- **Preconditions:**
  - Counterparty name was written at `as_of='2024-01-10T08:00:00Z'`,
    then redacted (a new row written) at
    `as_of='2025-03-15T12:00:00Z'`.
  - Test API key has `pii_unredacted=true` (so we can observe both
    states).
- **Steps:**
  1. GET event at `as_of=2024-12-01T00:00:00Z`.
  2. GET event at `as_of=2025-06-01T00:00:00Z`.
- **Expected:**
  - Step 1: returns the unredacted name (pre-redaction).
  - Step 2: returns `<REDACTED:gdpr_art_17>`.
- **Pass criterion:** Redaction is recorded as a new bitemporal row,
  not a destructive `UPDATE`. Moat 2 is preserved across the GDPR
  Article 17 surface.
- **Implementation:** integration

### TC-061: Redaction without `pii_unredacted` scope — both as_of redacted

- **Category:** GDPR / KVKK
- **Traces to:** SCOPE.md D30
- **Preconditions:** Same fixture; API key has `pii_unredacted=false`.
- **Steps:** Same two GETs as TC-060.
- **Expected:**
  - Both responses redacted; client cannot infer the value through
    bitemporal time-travel.
- **Pass criterion:** PIT does not become a side-channel for
  redacted data.
- **Implementation:** integration

---

## 12. Caching

### TC-062: Past-`as_of` returns immutable cache headers

- **Category:** Caching
- **Traces to:** SCOPE.md D17
- **Preconditions:** `as_of=NOW() - 30 days`.
- **Steps:**
  1. GET with that `as_of`.
- **Expected:**
  - `Cache-Control: public, max-age=31536000, immutable`.
  - `ETag: "<sha256-of-canonical-body>"`.
- **Pass criterion:** CDN can cache aggressively; headers are exact.
- **Implementation:** integration

### TC-063: Current `as_of` returns `no-cache`

- **Category:** Caching
- **Traces to:** SCOPE.md D17
- **Preconditions:** No `as_of` query param (defaults to NOW()).
- **Steps:**
  1. GET with no `as_of`.
- **Expected:**
  - `Cache-Control: no-cache`; `ETag` still present (for
    revalidation).
- **Pass criterion:** Current view is never cached publicly; ETag
  enables cheap conditional GET.
- **Implementation:** integration

### TC-064: `If-None-Match` returns 304 on past-`as_of` hit

- **Category:** Caching
- **Traces to:** SCOPE.md D17
- **Preconditions:** TC-062 has just run; client has the ETag.
- **Steps:**
  1. GET same URL with `If-None-Match: "<etag>"`.
- **Expected:**
  - HTTP 304 Not Modified; body empty; same ETag echoed.
- **Pass criterion:** Conditional revalidation works for past-as_of
  immutable responses.
- **Implementation:** integration

---

## 13. Schema-level invariants

### TC-065: `check_bitemporal_invariants.py` returns 0 on shadow

- **Category:** Schema-level invariants
- **Traces to:** SCOPE.md §6 (acceptance) item (2); D28
- **Preconditions:**
  - Shadow DB has all Phase 2 migrations applied.
- **Steps:**
  1. `python scripts/check_bitemporal_invariants.py --dsn=$SHADOW_DSN`.
- **Expected:**
  - Exit code 0; report lists every Class A/C table with status `ok`.
- **Pass criterion:** No invariant violation; the script's full
  inventory matches `bitemporal_table_registry`.
- **Implementation:** db-invariant

### TC-066: Every Class A table has its `BEFORE UPDATE` trigger

- **Category:** Schema-level invariants
- **Traces to:** SCOPE.md §4 (schema impact); D28
- **Preconditions:** Shadow DB with Phase 2 applied.
- **Steps:**
  1. Iterate the Class A table inventory from
     `EXISTING_PATTERNS_AUDIT.md`.
  2. For each, query `pg_trigger` for a trigger whose body raises
     `feature_not_supported`.
- **Expected:**
  - Every Class A table returns ≥1 matching trigger.
- **Pass criterion:** No table from the inventory is missing its
  trigger.
- **Implementation:** db-invariant

### TC-067: PIT function exists for every registry row exposed in API

- **Category:** Schema-level invariants
- **Traces to:** SCOPE.md D1, D28
- **Preconditions:** `bitemporal_table_registry` has rows for every
  table exposed in v1.
- **Steps:**
  1. For each row with `exposed_in_api=true`:
     a. Confirm `pit_function_name` resolves in `pg_proc`.
     b. Call the function with `p_as_of=NOW()` against the seeded
        line-item label `"Hasılat — Yurt İçi Satışlar"`.
- **Expected:**
  - Every function exists; every function returns rows; no
    schema-mismatch errors.
- **Pass criterion:** Registry, table, and function are in lockstep;
  Turkish content survives the function path byte-equal.
- **Implementation:** db-invariant

### TC-068: `bitemporal_table_registry` CI check fails on missing row

- **Category:** Schema-level invariants
- **Traces to:** SCOPE.md D28
- **Preconditions:**
  - Branch under test adds `as_of` to `agg.new_thing_table` without
    the corresponding registry row.
- **Steps:**
  1. `python scripts/check_bitemporal_table_registry.py --dsn=$SHADOW_DSN`.
- **Expected:**
  - Non-zero exit; report names `agg.new_thing_table` as a missing
    registration.
- **Pass criterion:** CI can not pass merges that drift
  bitemporality.
- **Implementation:** db-invariant

---

## 14. Replayability

### TC-069: 10 random `agg.filing_event` rows replay schema-equal

- **Category:** Replayability
- **Traces to:** SCOPE.md D29 (Replay); ADR-003 §"Replayability target"
- **Preconditions:**
  - `agg.filing_event` has ≥10 rows with
    `(prompt_version, model_identity, extraction_seed, raw_bytes_sha256)`
    populated.
- **Steps:**
  1. Pick 10 rows uniformly at random.
  2. Re-run extraction pipeline against original raw bytes with
     each row's recorded `(prompt_version, model, seed)`.
  3. Compare the re-extracted event to the stored event with a
     schema-equality predicate (per ADR-003: byte-equality where
     formatting is acceptable, schema-equality otherwise).
- **Expected:**
  - All 10 produce equal events.
- **Pass criterion:** Every event is reconstructable from raw +
  recorded versions; ≥1 failure flips the whole CI job red.
- **Implementation:** replay

### TC-070: Replay surfaces lineage in `metadata.lineage` for `agg.filing_event` API hits

- **Category:** Replayability
- **Traces to:** SCOPE.md D15
- **Preconditions:** Same fixtures as TC-069.
- **Steps:**
  1. GET `/v1/research/events?event_id=<id>`.
- **Expected:**
  - `metadata.lineage.source_filing_id`,
    `raw_bytes_sha256`, `extraction_code_version`,
    `prompt_version`, `model_identity`, `extraction_seed` all
    populated for each row in the response.
- **Pass criterion:** Lineage is contractually surfaced; replay
  consumers can pull these fields directly.
- **Implementation:** integration

---

## 15. Reversibility

### TC-071: alembic up→down→up cycle leaves zero schema diff

- **Category:** Reversibility
- **Traces to:** SCOPE.md §6 (acceptance) item (7); H6
- **Preconditions:** Shadow DB at the parent revision of the
  Phase 2 migrations.
- **Steps:**
  1. `alembic upgrade head`.
  2. Snapshot schema (`pg_dump -s`).
  3. `alembic downgrade -2` (down through the bitemporal triggers
     migration and the registry table migration).
  4. `alembic upgrade head`.
  5. Snapshot schema again.
  6. Diff the two snapshots.
- **Expected:**
  - Zero diff (modulo function OID or sequence values, which the
    test's diff filter normalizes).
- **Pass criterion:** The migration set is fully reversible at the
  schema level.
- **Implementation:** reversibility

### TC-072: alembic downgrade leaves zero data drift

- **Category:** Reversibility
- **Traces to:** SCOPE.md §6 (acceptance) item (7); H6
- **Preconditions:** Shadow DB seeded with representative data;
  Phase 2 head applied.
- **Steps:**
  1. Snapshot all data in `ts.observation`,
     `ts.financial_line_item`, `ts.canonical_financial`,
     `agg.filing_event`, `ref.entity`, `kap.disclosures` via
     row-level checksums.
  2. `alembic downgrade -2`.
  3. `alembic upgrade head`.
  4. Re-checksum.
- **Expected:**
  - Checksums match exactly; zero rows changed; zero rows lost.
- **Pass criterion:** Reversibility is data-safe, not just
  schema-safe.
- **Implementation:** reversibility

---

## 16. Feature flags

### TC-073: Master flag off → 503 on every endpoint

- **Category:** Feature flags
- **Traces to:** SCOPE.md D27, D16; SCOPE.md §3
- **Preconditions:** `BITEMPORAL_API_ENABLED=false`.
- **Steps:**
  1. GET each of the 8 endpoints in D1, plus `/v1/verify/moat-2`
     and `/v1/research/version`.
- **Expected:**
  - Every endpoint returns HTTP 503 `FEATURE_DISABLED`, EXCEPT:
    - `/v1/verify/moat-2` returns 503 with the same code (canary
      should reflect the disabled state, not a green answer).
    - `/v1/research/version` MAY still return 200 with feature_flags
      surfaced (so an operator can see the disabled state without
      auth) — this is implementation-defined; the test asserts the
      chosen behavior is documented in `IMPLEMENTATION_NOTES.md`.
- **Pass criterion:** Master kill-switch is comprehensive; no
  endpoint serves data when it is off.
- **Implementation:** feature-flag

### TC-074: `BITEMPORAL_API_INTERVAL_QUERIES=false` rejects `as_of_range`

- **Category:** Feature flags
- **Traces to:** SCOPE.md D2
- **Preconditions:**
  - Master flag on; `BITEMPORAL_API_INTERVAL_QUERIES=false`.
- **Steps:**
  1. GET with `as_of_range=[…)`.
  2. GET with `as_of=…` (point-in-time).
- **Expected:**
  - Step 1: HTTP 503 `FEATURE_DISABLED`; `extensions.flag` populated.
  - Step 2: HTTP 200.
- **Pass criterion:** Per-flag gating works; PIT path is unaffected
  when the interval flag is off.
- **Implementation:** feature-flag

### TC-075: `/v1/research/version` surfaces flag state

- **Category:** Feature flags
- **Traces to:** SCOPE.md D25, §3
- **Preconditions:** Some flags `true`, some `false`.
- **Steps:**
  1. GET `/v1/research/version`.
- **Expected:**
  - Response includes
    `feature_flags = {<flag_name>: <bool>, …}` covering every flag
    in SCOPE.md §3 (the 17-row table).
  - Includes `commit_sha`, `openapi_version`,
    `bitemporal_api_envelope_version`.
- **Pass criterion:** Operators and clients can introspect flag
  state without running Postgres queries.
- **Implementation:** feature-flag

### TC-076: Flag overrides via `aslan_core.feature_flags` table take effect at request boundary

- **Category:** Feature flags
- **Traces to:** SCOPE.md §3 (per-key overrides)
- **Preconditions:**
  - Process-level env says
    `BITEMPORAL_API_PII_EXPOSURE=false` but a row in
    `aslan_core.feature_flags` enables it for `api_key_id=<X>`.
- **Steps:**
  1. GET PII endpoint as that key.
  2. GET PII endpoint as a different key (no override).
- **Expected:**
  - Request 1: PII unredacted (per the override).
  - Request 2: redacted (per the env default).
- **Pass criterion:** Per-key overrides resolve correctly; the
  audit log records `feature_flags_active` exactly as resolved per
  request.
- **Implementation:** feature-flag

---

## Test data fixtures required

Each integration test depends on one or more of the following seed
fixtures. Fixture names are stable across test runs; pytest fixtures
under `aslan-core/tests/fixtures/bitemporal/` materialize them via
SQL files committed to the repo.

| Fixture | Contents | Used by |
|---|---|---|
| **A** `obs_garan_single` | One `ts.observation` row: `series_id='BIST.GARAN.close'`, `ts='2024-06-15T13:00:00Z'`, `as_of='2024-06-15T13:30:00Z'`, `value=42.10`. | TC-001, TC-005, TC-042, TC-046, TC-073 |
| **B** `obs_akbnk_three_versions` | Three `ts.observation` versions for `BIST.AKBNK.close` at `ts='2024-08-01T13:00:00Z'`. | TC-002, TC-014–TC-016, TC-018, TC-046–TC-049 |
| **C** `entity_42_finline_pre_bitemporal_floor` | `ts.financial_line_item` rows for `ASL-ENT-00000042`; earliest `as_of='2023-04-01T08:00:00Z'`. | TC-003, TC-021 |
| **D** `tas29_chain_revenue_42` | Two-row `ts.canonical_financial` chain for `(ASL-ENT-00000042, 2022-12-31, "revenue")`: nominal then TAS 29 restated; chain MV refreshed. | TC-009–TC-011, TC-022, TC-067 |
| **E** `disclosure_pre_bitemporal_turkish` | `kap.disclosures` row `kap-2018-44023`, `title="Olağan Genel Kurul Toplantısı Sonucu"`, `as_of=NULL`, `as_of_provenance='pre_bitemporal_unknown'`. | TC-007, TC-008, TC-017 |
| **F** `disclosure_amendment_chain_capraise` | `kap.disclosures` rows for `kap-2024-19044`, `title="Sermaye Artırımı — Bedelli Pay Alma Hakkı"`, with two `as_of` versions and an amendment fixture script. | TC-019, TC-037, TC-038 |
| **G** `identifier_ticker_rename_88` | `ref.identifier` two-row daterange chain for `ASL-ENT-00000088`, `BIST` namespace, `OLDC`→`NEWC` rename in May 2022. | TC-012 |
| **H** `identifier_ambiguous_overlap` | Deliberately overlapping `ref.identifier` rows installed via `SET session_replication_role='replica'` to bypass GiST EXCLUDE for the test only. | TC-013 |
| **I** `api_keys_matrix` | Four API keys: public-tier, partner-tier, internal-tier, partner with `pii_unredacted=true`; plus an expired and a revoked key. | TC-027–TC-031, TC-058, TC-059, TC-076 |
| **J** `rate_limit_partner` | Partner-tier API key with default quotas; exercises the 60s window. | TC-032–TC-036 |
| **K** `pii_event_sisecam_turkish` | `agg.filing_event` row with counterparty `"Şişe Cam Topluluğu A.Ş."` indexed in `agg.filing_event_pii_index`. | TC-058, TC-059 |
| **L** `pii_redaction_chain` | Two `agg.filing_event` `as_of` rows for the same `event_id`; the second carries `<REDACTED:gdpr_art_17>` per D30 Article 17. | TC-060, TC-061 |
| **M** `canary_known_amendments` | Hand-curated ≥10-entry amendment list (incl. `kap-2024-19044`) at `aslan-core/tests/fixtures/bitemporal/canary_known_amendments.json`. | TC-054–TC-057 |
| **N** `replay_random_filing_events` | 50 `agg.filing_event` rows with full lineage (`prompt_version`, `model_identity`, `extraction_seed`, `raw_bytes_sha256`) and matching raw blobs in `doc.filing_body`. | TC-069, TC-070 |
| **O** `large_finline_versions` | `ts.financial_line_item` for one entity with 75 distinct `as_of` versions; exercises cap + pagination. | TC-018, TC-046, TC-047 |
| **P** `corruption_harness` | Test-only DSL `with bypass_triggers(): conn.execute(...)` using `SET LOCAL session_replication_role='replica'`; refuses to run against non-localhost DSNs. | TC-013, TC-055 |
| **Q** `line_item_label_turkish` | `ts.canonical_financial` row with label `"Hasılat — Yurt İçi Satışlar"` to verify Turkish byte preservation through PIT functions. | TC-067 |
| **R** `feature_flags_overrides` | Two `aslan_core.feature_flags` rows: one enabling `BITEMPORAL_API_PII_EXPOSURE` per-key, one disabling it process-wide. | TC-076 |
| **S** `audit_log_seed` | Synthetic `aslan_core.api_query_audit` row replayable per TC-040, with `query_params_sha256` recorded. | TC-040 |
| **T** `shadow_db_phase2_baseline` | Shadow DB snapshot at the parent revision of Phase 2 migrations, plus a row-level checksum manifest of every Phase-2-affected table. | TC-071, TC-072 |

---

## Cross-reference matrix (decision → test cases)

| SCOPE.md decision | Test cases |
|---|---|
| D1 | TC-001, TC-002, TC-003, TC-004, TC-067, TC-073 |
| D2 | TC-014–TC-019, TC-074 |
| D3 | TC-007, TC-008, TC-017 |
| D5 | TC-027, TC-028, TC-029, TC-030, TC-031 |
| D6 | TC-032, TC-033, TC-034, TC-035, TC-036 |
| D7 | TC-046, TC-047, TC-048, TC-049, TC-051 |
| D8 | TC-009, TC-010, TC-011, TC-022 |
| D9 | TC-012, TC-013 |
| D10 | TC-024 |
| D11 | TC-025, TC-026, TC-037, TC-038 |
| D12 | TC-005, TC-006 |
| D13 | TC-041, TC-042, TC-043, TC-044, TC-045 |
| D14 | TC-007, TC-008 |
| D15 | TC-008, TC-039, TC-070 |
| D16 | TC-003, TC-027, TC-028, TC-034, TC-041, TC-045, TC-073 |
| D17 | TC-062, TC-063, TC-064 |
| D18 | TC-028, TC-031, TC-040, TC-059 |
| D19 | TC-018, TC-050, TC-051, TC-052, TC-053 |
| D20 | TC-054, TC-055, TC-056, TC-057 |
| D27 | TC-073 |
| D28 | TC-065, TC-066, TC-067, TC-068 |
| D29 | TC-020–TC-026, TC-069 |
| D30 | TC-030, TC-031, TC-058–TC-061 |
| ADR-003 §"Schema discipline" | TC-001, TC-002, TC-020, TC-021, TC-026 |
| ADR-003 §"Moat 2 canary" | TC-054, TC-055, TC-056, TC-057 |
| ADR-003 §"Replayability target" | TC-069, TC-070 |
| H1 (append-only enforcement) | TC-020–TC-026 |
| H3 / H7 (feature flags) | TC-036, TC-073, TC-074, TC-075, TC-076 |
| H6 (reversibility) | TC-071, TC-072 |
| Moat 4 (Turkish fidelity) | TC-007, TC-019, TC-058, TC-067 |
| Moat 5 (TAS 29) | TC-009, TC-010, TC-011 |

---

## Notes for the implementer

- The `bypass_triggers` test helper (Fixture P) MUST be defined in
  `aslan-core/tests/conftest.py` and MUST refuse to operate against
  any DSN that is not `localhost` or a testcontainer. A safety
  assertion at the top of the helper. Any production DSN trips the
  assertion before any SQL runs.
- The `corruption_harness` is test-only by design; CI MUST refuse to
  run it against staging or prod DSNs.
- Every integration test's seed SQL is committed under
  `aslan-core/tests/fixtures/bitemporal/*.sql` for human inspection.
  No fixture is generated by Faker; deterministic seeds only.
- `KNOWN_AMENDMENTS` (Fixture M) is hand-curated and reviewed by
  sidar. Drift between the fixture and the canary's expectations
  is a sidar-level escalation, not a maintenance task.
- The test plan does not define test-data for endpoints that will
  return empty in v1 (`doc.filing`, `agg.filing_event`,
  `kap.parsed_disclosures` per STATE.md). Those endpoints are
  exercised by smoke tests asserting `data == []` and the envelope
  is well-formed; not enumerated above as bitemporal-correctness
  cases because there is nothing to be correct *about* until those
  tables are populated by `aslan-event-extractor` M0/M1.
- All HTTP examples assume the staging deploy at
  `https://aslan-staging.example.invalid/v1/research/`. CI runs
  against testcontainers mounted at
  `http://127.0.0.1:<random>/v1/research/`.
