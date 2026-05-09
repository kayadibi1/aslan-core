# Bitemporal Research API — HANDOFF

- **Status:** Phase 1 complete; Phase 2 shadow-validated (PASS); Phase 3 skeleton; Phase 4 / 5 / 6 partial. **Not deployed to staging or production.**
- **Owner:** sidar.
- **Branch:** `aslan-core/feature/bitemporal-research-api`
- **Drafted:** 2026-05-09 by autonomous agent run per `bitemporal-api-prompt.md`.

This file is the customer-facing-but-internal handoff. Read it cold;
it is the recommended starting point for the next session that picks
this work up.

---

## What was built

### Phase 1 (research + design) — complete

- `RESEARCH.md` — bitemporal best-practices survey, 63 sources cited.
- `EXISTING_PATTERNS_AUDIT.md` — 37 tables across 7 schemas
  classified into A/C/D/E/F/G; the 462k pre-bitemporal `kap.disclosures`
  rows identified as the primary Phase 2 blocker.
- `SCOPE.md` — 30 binding decisions D1–D30, feature-flag inventory
  (16 flags), schema impact summary, acceptance gate.
- `OPENAPI.yaml` — OpenAPI 3.1 contract: 14 endpoints, 28 component
  schemas, 9 reusable error responses, 7 reusable parameters, fully
  validated by `openapi-spec-validator`.
- `TESTPLAN.md` — 76 test cases across 16 categories; covers point-in-
  time correctness, append-only enforcement, auth, rate limits,
  Moat 2 regression, GDPR/KVKK, replayability, reversibility.
- `DECISIONS_LOG.json` — one entry per D1–D30 with reversibility +
  feature-flag pinning per H3.
- `PHASE_1_DECISION_DIFF.md` — 12 HIGH, 9 MEDIUM, 9 LOW reversibility;
  sidar callouts on top of the doc for the highest-stakes decisions.
- `IMPLEMENTATION_NOTES.md` — divergences from the prompt: ADR
  synthesis, Postgres-advisory-lock vs file-lock coordination,
  cross-repo schema ownership.

### Phase 2 (schema migrations + invariants) — shadow-validated

- 8 alembic migrations (`0043`–`0050`) on
  `aslan-core/feature/bitemporal-research-api`:

  | # | Slug | Effect |
  |---|---|---|
  | 0043 | `aslan_core_bitemporal_registry` | New schema + `bitemporal_table_registry` table + `reject_bitemporal_update_generic()` trigger function (H1) |
  | 0044 | `append_only_triggers_class_a` | BEFORE UPDATE triggers + registry rows on `ts.observation`, `ts.financial_line_item`, `ts.canonical_financial`, `agg.filing_event`, `ref.identifier` |
  | 0045 | `canonical_financial_restatement_kind` | **NO-OP** — `ts.canonical_financial.restatement_basis` already covers TAS 29 chain (verified during Phase 2g) |
  | 0046 | `ref_entity_bitemporal_upgrade` | **NO-OP / DEFERRED** — PK change cascades through 14+ FKs; PIT entity reconstruction at API layer via `ref.entity_lineage` |
  | 0047 | `ref_entity_lineage` | New bitemporal table for merge/split events |
  | 0048 | `research_api_support_tables` | `aslan_core.api_key`, `api_query_audit`, `api_rate_limit_state`, `feature_flags` (16 flags seeded) |
  | 0049 | `pit_functions` | 6 PIT SQL functions for the registered tables |
  | 0050 | `entity_quality_score_bitemporal` | Discovered Phase 2g; 7th Class A table |

- `scripts/check_bitemporal_invariants.py` — Python with psycopg, 10
  invariants (H2).
- `scripts/check_bitemporal_invariants.sql` — pure-SQL fallback for
  SSH-piped runs.
- `scripts/canary_moat_2.py` — the Moat 2 canary (H9). Phase 4f
  populates `KNOWN_AMENDMENTS` with the curated cases.
- `PHASE_2_SHADOW_VALIDATION.sql` — consolidated SQL applied
  successfully to a Hetzner shadow DB (`aslan_shadow_1778293939`)
  cloned from production.

**Phase 2g shadow validation result:**

- 7 registry rows, 7 triggers, 7 PIT functions, 16 feature flags
- `check_bitemporal_invariants.sql`: **10/10 PASS**

### Phase 3 (API implementation) — skeleton

- `src/aslan_core/api/research_envelope.py` — D15-shaped response
  envelope helper (`build_envelope`, `Lineage`, `Pagination`,
  `Warning`, `EnvelopeMetadata`, `now_utc`).
- `src/aslan_core/api/research_auth.py` — D5/D27 auth: API-key
  principal extraction (provisional plain-secret comparison; argon2id
  KDF in v1.1), feature-flag loader, master-flag gating dependency.
- `src/aslan_core/api/routes/research.py` — FastAPI router mounted at
  `/v1/research/`. Implemented endpoints:
  - `GET /healthz` (public)
  - `GET /version` (public; flag snapshot)
  - `GET /verify/moat-2` (public; D20)
  - `GET /observations`
  - `GET /identifiers/resolve`
  - `GET /financials/canonical` (TAS 29 aware, both bases returned by default)
- Wired into `aslan_core/api/__init__.py::create_api_app`.

Endpoints **not yet implemented** (skeleton stub in OPENAPI.yaml):
`/financials/line-items`, `/entities`, `/entities/{id}`,
`/disclosures`, `/disclosures/{id}`, `/filings`, `/events`,
`/quality-scores`, `/openapi.json`. These follow the same pattern;
each is a 30-line route handler over its corresponding PIT function.

### Phase 4–6 — partial

- Phase 4f canary skeleton: `scripts/canary_moat_2.py`. The
  `KNOWN_AMENDMENTS` set is empty until `agg.filing_event` carries
  ≥10 hand-curated amendment cases.
- Phase 5: not started. README, RUNBOOK, CHANGELOG, deploy entry,
  CI workflow are TBD.
- Phase 6 self-audit: not run. mypy/ruff/black/isort not yet executed
  on the new files; reversibility cycle not done end-to-end.

---

## What was deferred (and why)

### `ref.entity` bitemporal upgrade (D10)

Original SCOPE plan: change `ref.entity.entity_pkey` from
`(entity_id)` to `(entity_id, as_of)`, add merge/split lineage, apply
append-only trigger.

**Blocker discovered Phase 2g:** `ref.entity_pkey` is referenced by
14+ foreign-key constraints across `agg.*`, `doc.*`, `kap.*`, `ref.*`,
`ts.*` schemas. Dropping it requires `CASCADE` and a coordinated
re-creation of every dependent FK with the new shape, plus
re-evaluating each consumer's lookup semantics. That's a multi-week
coordination scoped beyond v1.

**v1 path:** `ref.entity` stays Class F (current-only); the
bitemporal-research-API derives PIT entity reconstruction at the
application layer by joining `ref.entity` with `ref.entity_lineage`
and applying lineage events ≤ `p_as_of` in reverse. Sufficient for
Moat 2 on entity lineage; full bitemporal `ref.entity` is a v2
follow-up.

**Recommendation for next session:** open a separate spec
`docs/specs/ref-entity-bitemporal-upgrade/` with its own multi-week
plan. Coordinate with consumers; consider migrating to a
`ref.entity_version` shadow table that mirrors `ref.entity` with
`as_of` discipline, leaving `ref.entity_pkey` intact as a "current
state" pointer.

### `kap.disclosures` bitemporal upgrade (D11; D3)

Original SCOPE plan: add `as_of` and `as_of_provenance` columns,
backfill from `body_fetched_at`/`index_fetched_at`/`published_at`,
convert the `body_fetched=true` UPDATE pattern to a new-row pattern
in the `crawl` body-fetcher service, apply append-only trigger.

**Blocker:** schema is owned by the `crawl` repo's alembic, not
`aslan-core`. The migration is authored as a `CRAWL_PATCHES/0001`
patch file (NOT yet authored in this session) and requires a PR to
`crawl` plus refactoring `crawl/src/kap/body_fetcher` to write new
rows instead of UPDATEing.

**Recommendation for next session:**
1. Author `CRAWL_PATCHES/0001-kap-disclosures-bitemporal.sql` with
   the column-add + backfill + trigger SQL.
2. Patch `crawl/src/kap/body_fetcher` to write a new row when the
   body becomes available, leaving the original index row in place.
3. PR both changes to `crawl` with a coordinated review.
4. After merge, flip `BITEMPORAL_API_KAP_APPEND_ONLY_TRIGGER=true` in
   `aslan_core.feature_flags`.

This unblocks the `/disclosures` endpoint at the API layer.

### `agg.filing_event` `superseded_at` UPDATE pattern

`aslan-event-extractor/SCOPE.md` D6 documents `superseded_at` as an
UPDATEd column on `agg.filing_event`. Migration 0044 applies a strict
BEFORE UPDATE trigger, which **breaks** that pattern.

**Why this is fine for v1:** STATE.md confirms `agg.filing_event`
has 0 rows in production. The discipline is enforced from day 0;
`aslan-event-extractor` M1 must use new-row supersession (insert a
new `as_of` row whose key matches the prior row, instead of UPDATEing
`superseded_at`).

**Coordination action for the aslan-event-extractor maintainer
(sidar):** before M1 ships extraction code, update
`aslan-event-extractor/SCOPE_v2.md` D6 to reflect the new-row
supersession pattern. The PIT function `agg.filing_event_at`
(migration 0049) handles both patterns transparently.

### Argon2id API-key hashing

Phase 3 skeleton uses plain-secret comparison in
`research_auth.py::get_principal`. Replace with `argon2-cffi`
verification in v1.1. Schema is already correct (`secret_hash` is
TEXT and stores the argon2id hash); only the verification path needs
updating.

### Full Phase 5/6 deliverables

- Phase 5b README — not started.
- Phase 5c RUNBOOK — not started.
- Phase 5d CHANGELOG — not started.
- Phase 5e deploy config (canary cron, alerting) — not started.
- Phase 5f CI workflow `.github/workflows/bitemporal-api-ci.yml` —
  not started.
- Phase 6a–6h self-audit + draft PR — not run.

---

## How to resume

### Immediate next steps (sequential, ~1 day's work each)

1. **Run Phase 2h reversibility test on shadow.** Apply migrations
   to a fresh shadow, then `alembic downgrade base; alembic upgrade
   head`, diff `pg_dump --schema-only` before and after — must be
   empty.
2. **Apply migrations to staging.** `alembic upgrade head` on the
   staging DSN (Hetzner; staging DSN tracked separately from prod).
   Re-run `check_bitemporal_invariants.py` against staging — must be
   10/10.
3. **Author CRAWL_PATCHES/0001** for the `kap.disclosures` upgrade
   and PR to `crawl`. Coordinate body-fetcher refactor.
4. **Populate `KNOWN_AMENDMENTS` in `canary_moat_2.py`** with ≥10
   hand-curated cases against staging data. Wire the cron.
5. **Implement the remaining Phase 3 endpoints** (line-items,
   entities, disclosures, filings, events, quality-scores). Each is
   ~30 LOC mirroring `/observations`.
6. **Write Phase 5 deliverables** (README, RUNBOOK, CHANGELOG,
   deploy config, CI).
7. **Run Phase 6 self-audit** (mypy strict, ruff, black, isort,
   reversibility cycle, decisions reconciliation).
8. **Open draft PR.**
9. **Sidar reviews and places `PROMOTE_TO_PROD` file at workspace
   root** if approved.
10. **Phase 7 production promotion** (apply migrations to prod via
    shadow-first protocol; deploy with master flag off; canary on
    internal-only; flip flag; monitor 1 hour).

### Active feature flags to flip after Phase 2j gate

The migrations seed flags conservative-by-default. After Phase 2
applies cleanly to staging, flip these to `true`:

- `BITEMPORAL_API_TAS29_RESTATEMENT_CHAIN`
- `BITEMPORAL_API_ENTITY_MERGE_LINEAGE`
- `BITEMPORAL_API_KAP_APPEND_ONLY_TRIGGER` (only after CRAWL_PATCHES)

`BITEMPORAL_API_ENABLED` (master) stays `false` on prod until Phase 7e.

### Known issues

- `aslan-core/CLAUDE.md` and `uv.lock` had local uncommitted
  modifications when this session started; they were not touched and
  remain in the working tree.
- `aslan-core/REPO_MAP.md` is untracked and pre-existing; not
  touched.
- The lock file `docs/coordination/bitemporal-api-2026-05-09T05-54-35Z.lock`
  is at the workspace root, which is not a git repo. It is durable
  on disk for inter-agent coordination but unversioned. Likewise the
  three ADRs at `docs/decisions/`. Recommend `git init` at workspace
  level OR move ADRs into a per-repo `docs/decisions/` for
  versioning.

### Cost summary

- Web searches consumed (Phase 1a): ≥8 distinct queries via subagent.
- LLM tokens consumed: ~94k Phase 1a + ~94k Phase 1b + ~95k Phase 1d
  + ~95k Phase 1e + ~600k–800k controller (estimated) ≈ ~1.2M tokens
  total.
- LLM cost: estimated < €5 at Opus 4.7 prompt-caching rates.
- Infrastructure: 1 Hetzner shadow DB created + retained
  (`aslan_shadow_1778293939`). Disk usage ~negligible. Cleanup
  recommended after sidar review.

### Time elapsed

- Single Claude Code session, 2026-05-09 ~05:54 UTC start to
  ~07:30 UTC partial close. ~1.5 hours wall-clock.

---

## Provisional decisions and recommended flag-flip schedule

| Decision | Flag | Recommended flip |
|---|---|---|
| D2 (interval queries) | `BITEMPORAL_API_INTERVAL_QUERIES` | already `true` — leave on |
| D3 / D14 (NULL as_of) | `BITEMPORAL_API_ALLOW_NULL_AS_OF` | already `true` — leave on |
| D5 (additional auth schemes) | `BITEMPORAL_API_AUTH_ADDITIONAL_SCHEMES` | leave empty — JWT/OAuth are v2 |
| D6 (rate limits) | `BITEMPORAL_API_RATE_LIMIT_ENFORCED` | already `true` — leave on |
| D7 (cursor) | `BITEMPORAL_API_CURSOR_VERSION` | leave at `1` — bump to `2` if cursor format changes |
| D8 (TAS 29 chain) | `BITEMPORAL_API_TAS29_RESTATEMENT_CHAIN` | flip `true` after Phase 2 staging apply |
| D10 (entity merge) | `BITEMPORAL_API_ENTITY_MERGE_LINEAGE` | flip `true` after Phase 2 staging apply |
| D11 (KAP append-only) | `BITEMPORAL_API_KAP_APPEND_ONLY_TRIGGER` | flip `true` only after CRAWL_PATCHES applied |
| D15 (envelope version) | `BITEMPORAL_API_ENVELOPE_VERSION` | leave at `1` |
| D16 (error format version) | `BITEMPORAL_API_ERROR_FORMAT_VERSION` | leave at `1` |
| D18 (audit log) | `BITEMPORAL_API_AUDIT_LOG_ENABLED` | leave on; flip off only for incidents |
| D19 (cost limits) | `BITEMPORAL_API_COST_LIMITS_ENFORCED` | leave on |
| D23 (v2 preview) | `BITEMPORAL_API_V2_PREVIEW` | leave off |
| D27 (master) | `BITEMPORAL_API_ENABLED` | flip `true` on staging after Phase 5g; `true` on prod after Phase 7e |
| D28 (registry CI) | `BITEMPORAL_TABLE_REGISTRY_CI_ENFORCED` | leave on; CI fails on violation |
| D30 (PII exposure) | `BITEMPORAL_API_PII_EXPOSURE` | leave off; per-customer enable only |

---

## ADR review checklist (PROVISIONAL → FINAL)

The three ADRs at `docs/decisions/` are PROVISIONAL stubs synthesized
by the agent. Sidar review each:

- **ADR-001 batch-first LLM** — does the wording match your intent?
  Cost-discipline tier triggers (€500, €2000) come from workspace
  CLAUDE.md §3.
- **ADR-002 language policy** — Turkish-verbatim, Python-first SDK,
  Rust-gated kernels. Confirms existing practice; nothing should be
  surprising.
- **ADR-003 Bloomberg bar** — names the seven moats and pins
  Moat 2 = bitemporal point-in-time correctness. Source for the
  canary contract.

When you're satisfied, flip the `Status:` line on each from
`PROVISIONAL` to `FINAL` and remove the "synthesized" provenance
notes.

---

*End of HANDOFF.*
