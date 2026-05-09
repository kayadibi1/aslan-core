# aslan-core bitemporal patterns audit — 2026-05-09

This is the empirical input to Phase 2 schema migration design.
Compiled from a read-only sweep of the `aslan-core` and `crawl`
alembic migrations, ORM definitions, and `STATE.md` row counts.
**37 tables** found across 7 schemas.

## Summary table

| Schema.Table | Class | Bitemporal cols | Append-only enforced | PIT pattern | Live row count | Regression risk |
|---|---|---|---|---|---|---|
| `ts.observation` | A — fully bitemporal | `as_of` PK | yes (PK structure) | `DISTINCT ON (series_id, ts) ... ORDER BY series_id, ts, as_of DESC` | 7.8M | none |
| `ts.financial_line_item` | A | `as_of` PK; UNIQUE `(entity_id, filing_id, statement_type, line_code, consolidation, period_end, as_of)` | yes | inherits `ts.observation` pattern | 7.3M | none |
| `ts.canonical_financial` | A | `as_of` PK | yes | inherits | 1.7M | none |
| `agg.filing_event` | A | `as_of` + `superseded_at`; UNIQUE `(filing_id, event_type, event_seq, as_of)` | yes | view `agg.filing_event_current` per `aslan-event-extractor/SCOPE.md` §5.1 | small (new) | none |
| `ref.identifier` | C — daterange + GiST | `tstzrange(valid_from, valid_to, '[)')` + GiST EXCLUDE | EXCLUDE prevents overlaps | `WHERE namespace=? AND value=? AND daterange @> as_of` | small | low |
| `ref.entity` | F — reference | `created_at`, `updated_at` | no | n/a | 775 | low — rare updates but no `as_of` |
| `ref.entity_relationship` | F | `valid_from`, `valid_to` | no `as_of` discipline | implicit | small | medium — daterange present but no insert-only |
| `ref.entity_sector` | F | `valid_from`, `valid_to` | no `as_of` | implicit | small | medium — same as above |
| `ref.sector`, `ref.calendar`, `ref.calendar_day`, `ref.currency` | F | static | n/a | n/a | small | none |
| `kap.disclosures` | E + G | `published_at`, `index_fetched_at`, `body_fetched_at`, `body_fetched` (UPDATEd) | **no — `body_fetched` is UPDATEd** | none | **462,018** | **HIGH — pre-bitemporal mass + active mutation** |
| `kap.disclosure_attachments` | D | none | append-only by convention | n/a | small | low |
| `kap.parsed_disclosures` | D | none — appended after parse | by convention | n/a | 0 | low |
| `kap.disclosure_parse_state`, `kap.tail_state`, `kap.tail_unresolved_disclosures`, `kap.financial_parse_state`, `kap.standardize_quarantine`, `kap.attachment_pages` | D | n/a (job state / quarantine) | by convention | n/a | small | low |
| `audit.events` (hypertable) | D | timestamp + event_id; write-once | by design | n/a | moderate | none |
| `audit.observation_batch_keys` | D | append-only audit | by design | n/a | small | none |
| `streams.outbox`, `streams.deadletter_log`, `streams.deadletter_redis_index`, `streams.deadletter_xadd_intent`, `streams.event_id_to_redis` | D | append-only | by design | n/a | varies | none |
| `streams.redaction_registry` | F | static reference / mutable config | n/a | n/a | small | low |
| `src.source`, `src.ingestion_run`, `src.watermark` | F | bookkeeping | n/a | n/a | small | none |
| `doc.filing`, `doc.filing_attachment`, `doc.filing_body` | F (currently empty) | immutable on creation | by design | n/a | 0 | none |
| `auth.user`, `auth.refresh_token` | F | session state | n/a | n/a | small | low |
| `ts.series_catalog`, `ts.series_subject` | F | catalog/metadata | rare updates | n/a | small | low |
| `agg.restatement_config` | F (config) | static | n/a | n/a | small | none |
| `agg.entity_latest_snapshot` | F (materialized view, refreshed nightly) | n/a | not bitemporal — derived | n/a | small | n/a |
| `agg.observation_daily_to_monthly` | F (continuous aggregate) | derived | n/a | n/a | varies | n/a |
| `agg.filing_event_quarantine` | D | append-only | by design | n/a | small | low |
| `agg.filing_event_review_queue`, `agg.filing_event_extraction_state`, `agg.filing_event_label`, `agg.entity_resolution_queue` | F (work queue / labeling) | mutable transient | n/a | n/a | small | medium — UPDATE-driven |
| `agg.filing_event_pii_index` | D | append-only index | by design | per `agg.filing_event` | small | none |

**Class totals:** A=4, C=1, D=13, E=1, F=14, G=1 (`kap.disclosures` straddles E and G).

## Per-table detail

### `ts.observation` (Class A)

- **Migration file:** aslan-core `aslan_core/db/migrations/versions/` (ts schema).
- **Bitemporal columns:** `as_of timestamptz` as part of PK; `(series_id, ts, as_of)`.
- **Append-only enforcement:** PK structure forces unique `(series_id, ts, as_of)`. No explicit BEFORE UPDATE trigger.
- **PIT pattern:** Documented in DATABASE_ARCHITECTURE.md §4.4 + `aslan-core` codex prompt: `SELECT DISTINCT ON (series_id, ts) ... WHERE as_of <= :tt ORDER BY series_id, ts, as_of DESC`.
- **Live rows:** 7.8M.
- **Risk:** none.
- **Phase 2 action:** add explicit BEFORE UPDATE trigger that raises `feature_not_supported` (defense in depth — PK uniqueness is necessary but not sufficient against a buggy UPDATE that doesn't change the PK columns).

### `ts.financial_line_item` (Class A)

- Bitemporal columns: `as_of timestamptz` in PK.
- Phase 2 action: add BEFORE UPDATE trigger.

### `ts.canonical_financial` (Class A)

- Bitemporal columns: `as_of timestamptz` in PK.
- Phase 2 action: add BEFORE UPDATE trigger; add `restatement_kind text` for TAS 29 (per SCOPE.md D8).

### `agg.filing_event` (Class A)

- Bitemporal columns: `as_of` + `superseded_at`.
- View `agg.filing_event_current` projects current state.
- Phase 2 action: add BEFORE UPDATE trigger (the `superseded_at` field is itself UPDATEd today per `aslan-event-extractor/SCOPE.md` D6 — Phase 2 must convert that to a new-row pattern OR allow updates to `superseded_at` only via a column-level trigger that gates other columns).

### `ref.identifier` (Class C)

- Daterange + GiST EXCLUDE established in `aslan_core/db/migrations/versions/20260427_1612_0002_ref_schema_and_tables.py`.
- PIT pattern: `WHERE namespace=? AND value=? AND daterange @> :as_of`.
- Phase 2 action: add BEFORE UPDATE trigger; the GiST EXCLUDE prevents overlapping daterange but does not prevent in-place UPDATE of an existing row's contents.

### `ref.entity` (Class F → must upgrade to A for Moat 2)

- Currently no `as_of`; uses `created_at`, `updated_at`.
- Phase 2 action: add `as_of timestamptz NOT NULL DEFAULT now()`, `merged_from_entity_ids jsonb`, BEFORE UPDATE trigger. Backfill `as_of = created_at` for existing 775 rows. Per SCOPE.md D10.

### `ref.entity_relationship`, `ref.entity_sector` (Class F → C-shape upgrade)

- Have `valid_from/valid_to` but no `as_of` for transaction-time discipline.
- Phase 2 action: add `as_of`, append-only trigger, OR convert to `tstzrange daterange + GiST EXCLUDE` matching `ref.identifier`. Decision deferred to Phase 2 design review.

### `kap.disclosures` (Classes E + G — the primary blocker)

**462,018 rows, mutable, pre-bitemporal.** The single biggest Phase 2
risk.

| Column | Type | Provenance |
|---|---|---|
| `published_at` | timestamptz | Official KAP publication time. Immutable per disclosure. Best `as_of` proxy for the original publication. |
| `index_fetched_at` | timestamptz | When we fetched the index listing. Mutable across re-runs. |
| `body_fetched_at` | timestamptz | When we fetched the body. NULL until fetch. Added in M5. |
| `body_fetched` | bool | **UPDATEd on body fetch** — the active mutation path. |
| `received_at` | (not present) |  |
| `inserted_at` | (not present) |  |
| `fetched_at` | (not present) |  |

**Phase 2 strategy** (per SCOPE.md D3):

1. Add `as_of timestamptz NULL` and `as_of_provenance text NULL`
   columns to `kap.disclosures`.
2. Backfill: `as_of = COALESCE(body_fetched_at, index_fetched_at,
   published_at)`, `as_of_provenance` set accordingly.
3. The 89,700 rows with `body_fetched=true` get
   `as_of_provenance='body_fetched_at'`.
4. The remaining ~372k rows where `body_fetched=false` get
   `as_of_provenance='index_fetched_at'` if non-null, else
   `'published_at'`, else `'pre_bitemporal_unknown'` with `as_of` left
   NULL.
5. Convert the `body_fetched=true` UPDATE pattern to a new-row
   pattern: instead of `UPDATE kap.disclosures SET body_fetched=true,
   body_fetched_at=now()` (mutation), insert a new row with
   the body-fetch state. **This is a Phase 2 migration coordinated
   with `crawl`'s body-fetcher service** (per IMPLEMENTATION_NOTES.md
   "Schemas with no aslan-core ownership").
6. After migration, add BEFORE UPDATE trigger.

This is the single biggest piece of Phase 2 work and the highest-
risk deploy.

## The 462k pre-bitemporal `kap.disclosures` problem (focused subsection)

Recommended `as_of` derivation, in priority order:

1. `body_fetched_at` if `body_fetched=true` (89,700 rows)
2. `index_fetched_at` if non-null (likely most of the rest)
3. `published_at` (always non-null per KAP — but represents publication, not platform-knowledge)
4. NULL with `as_of_provenance='pre_bitemporal_unknown'` (rare)

`published_at` is the closest thing to a guaranteed timestamp but
is **publication time** (KAP's clock), not **platform-known time**.
For pre-bitemporal rows we accept this divergence and document it.

## Cross-cutting findings

- **Bitemporal pattern A** (PK includes `as_of`) is the dominant
  pattern in `ts.*` and `agg.filing_event`. **Reuse this** for new
  bitemporal tables.
- **Bitemporal pattern C** (daterange + GiST EXCLUDE) is used
  exclusively in `ref.identifier`. Apt for slow-changing
  identifiers. Not a substitute for pattern A on fact tables.
- The `agg.*` table family has the most heterogeneous discipline:
  `agg.filing_event` is fully bitemporal (A); the queues
  (`review_queue`, `extraction_state`, `entity_resolution_queue`)
  are intentionally mutable transients. They are not exposed by
  the bitemporal API; if they ever are, they need conversion.
- `audit.events` and `streams.*` are append-only by design and
  don't need bitemporal upgrades — they are themselves the audit
  layer.

## Recommended Phase 2 migration order (low-risk-first)

1. **Add `aslan_core.bitemporal_table_registry`** (per SCOPE.md
   D28). Empty initially; populated as triggers ship.
2. **Add BEFORE UPDATE triggers** to Class A tables (`ts.observation`,
   `ts.financial_line_item`, `ts.canonical_financial`,
   `agg.filing_event`, `ref.identifier`). No data change; pure
   discipline. Reversibility HIGH.
3. **Add `restatement_kind`** to `ts.canonical_financial` (per D8).
4. **Add `as_of`, `merged_from_entity_ids`** to `ref.entity` and
   backfill from `created_at`. Add trigger.
5. **Author `ref.entity_lineage` table** (per D10).
6. **Add `as_of`, `as_of_provenance`** to `kap.disclosures` and
   backfill (the big one). Coordinate with crawl. Add trigger.
7. **Migrate `ref.entity_relationship`, `ref.entity_sector`** to
   either daterange-EXCLUDE or `as_of`-PK. Decide in Phase 2 design
   review.
8. **Add the API support tables**: `aslan_core.api_key`,
   `api_query_audit`, `api_rate_limit_state`, `feature_flags`.
9. **Author PIT functions** for every Class A and Class C table
   exposed in v1 (per D1).

Each step is a separate alembic revision per
`aslan-core/CLAUDE.md` "one logical change per revision".

## What's NOT covered here

- ORM models (in `aslan_core.models`) for the affected tables — Phase 2
  updates them per the migration; not catalogued here.
- The `streams.redaction_registry` PII-redaction interaction with
  bitemporal queries — covered in SCOPE.md D30.
- The `agg.filing_event` `superseded_at` UPDATE pattern's interaction
  with append-only triggers — flagged as a decision item for Phase 2
  design review.
