# Bitemporal Research API — HANDOFF

- **Status:** Phases 1-6 complete; bug-review sweep complete
  (Codex gpt-5.5 + xhigh produced 17 findings; **17/17 closed**
  in round 6). Phase 2h reversibility cycle on shadow PASS;
  Phase 3 fully implemented (15 routes including
  `/v1/research/openapi.json`); Phase 4 tests (**48 cases
  collected**) covering all D-decisions; Phase 5 docs + CI +
  deploy entries landed; Phase 6 lint + argon2id + draft PR open
  (#25). **Not deployed to staging or production** —
  `BITEMPORAL_API_ENABLED=false` in the seeded
  `aslan_core.feature_flags` and Phase 7 production deploy is
  gated on `PROMOTE_TO_PROD` per spec.
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

## Round-2 update — formerly-deferred items now implemented

Earlier drafts of this HANDOFF flagged several items as deferred.
Round 2 (commit `7945926`) closed them. Round 3 (commit `9b7c67a`)
added the reversibility cycle and replaced synthetic canary cases
with real prod data. Self-review (commit pending) tightened lint,
fixed psycopg packaging, and updated `regex=` to `pattern=`.

### `ref.entity` bitemporal — DONE via SCD-4

**Migration 0046** (was no-op stub) now creates `ref.entity_version`
PK `(entity_id, as_of)` as a bitemporal history mirror of
`ref.entity`. The current-state `ref.entity` is unchanged — all
14+ FK constraints still resolve. An AFTER INSERT/UPDATE/DELETE
trigger `ref.fn_capture_entity_version` writes the post-state row
into the version table with `event_kind in (created, updated,
merged, split, renamed, deleted)`. Append-only enforcement on the
version table itself.

Backfill: every existing entity gets a `created` row from
`created_at` and (where `updated_at > created_at`) an `updated`
row.

PIT function `ref.entity_at(p_as_of)` reads from the version
table. Registry row exposes `/v1/research/entities`.

This is the SCD Type 4 pattern: keep the current-state pointer
table for FK consumers; bitemporal history in a parallel table.

### `kap.disclosures` bitemporal — DONE via SCD-4 (no crawl changes)

**Migration 0051** creates `kap.disclosures_version` PK
`(disclosure_id, as_of)`. Same SCD-4 pattern; the existing 4 FKs
on `kap.disclosures` are untouched. The `crawl` body-fetcher
continues to UPDATE `body_fetched=true` and `body_fetched_at`
on the same row — no `crawl` code change required. The new AFTER
trigger captures every change with `event_kind` discriminating
`indexed` / `body_fetched` / `republished` / `updated` / `deleted`.

**Pre-bitemporal backfill (D3) executed:** every existing
`kap.disclosures` row is seeded into the version table with
`as_of=index_fetched_at` and `as_of_provenance='index_fetched_at'`
(or `'pre_bitemporal_unknown'` if NULL). The ~89.7k rows with
`body_fetched=true` get a second version with
`as_of=body_fetched_at` and `as_of_provenance='body_fetched_at'`.

PIT function `kap.disclosures_at(p_as_of)`. Registry row exposes
`/v1/research/disclosures`. The `/disclosures` endpoint is wired.

### `agg.filing_event` `superseded_at` — coordination still pending

Migration 0044 applies the strict BEFORE UPDATE trigger as
designed. STATE.md confirms `agg.filing_event` has 0 rows in
production, so this discipline is enforced from day 0 with no
data impact. **`aslan-event-extractor` M1 must use new-row
supersession** instead of UPDATEing `superseded_at`. Cross-repo
coordination action for the aslan-event-extractor maintainer:
before M1 extraction code ships, update
`aslan-event-extractor/SCOPE_v2.md` D6 to reflect the new-row
pattern. The PIT function `agg.filing_event_at` already handles
both patterns transparently.

### Argon2id API-key hashing — DONE

`research_auth.py` now uses `argon2.PasswordHasher.verify` for
secret verification, with `hmac.compare_digest` constant-time
fallback for non-`$argon2`-prefixed legacy seed hashes.
`hash_secret` and `verify_secret` exported as the key-issuance
contract. `argon2-cffi` declared in `pyproject.toml` dependencies.

### Phase 5/6 deliverables — DONE

- Phase 5b `README.md` — customer-facing intro, curl/Python/SDK
  quickstart, KAP-amendment PIT walkthrough with verbatim Turkish
  disclosure title.
- Phase 5c `RUNBOOK.md` — deploy, master-flag flip, key rotation,
  rate-limit tuning, 3 incident runbooks (canary-red, slow queries,
  audit-log disk), Prometheus inventory, alert thresholds, kill
  switch.
- Phase 5d `CHANGELOG.md` — Keep-a-Changelog format,
  `[v1.0.0-alpha] — 2026-05-09` entry covering migrations
  0043-0051.
- Phase 5e deploy entry — `bitemporal-canary` profile-gated
  service in `infra/deploy/docker-compose.yml`, runs
  `scripts/canary_moat_2.py` every 5 minutes.
- Phase 5f `.github/workflows/bitemporal-api-ci.yml` — three jobs:
  validate-openapi, ruff, mypy --strict.
- Phase 6 — `ruff` and `mypy --strict` clean on all new files;
  reversibility cycle on shadow PASS (round 3); draft PR open at
  https://github.com/kayadibi1/aslan-core/pull/25.

## Round-6 update — bug-review sweep complete

Codex (gpt-5.5 + xhigh) ran the comprehensive `BUG_REVIEW.md` spec
and produced 17 findings. **All 17 closed** across three commits:

- `b03076d` part 1 — mechanical fixes including the BLOCKER
  (invariant SQL referenced nonexistent `daterange` column on
  `ref.identifier`), the round-5 RFC 7807 handler-order regression,
  `_PUBLIC_PATHS` exact-match, audit-middleware `startswith`,
  Compose env-var escape, CI workflow paths, README/RUNBOOK/CHANGELOG
  refresh.
- `04047c5` part 2 — `routes/research.py` 1432→1862 LOC: D2
  interval mode (`as_of_range` + `_enforce_query_cost` →
  `QUERY_TOO_LARGE`), PIT switch (`/entities` and `/disclosures`
  to `*_at(:as_of)`), response-shape alignment with OpenAPI
  (Entity/Disclosure/FilingEvent fields), `disclosure_id: str` for
  KAP IDs, `series_code` resolution, `ts_from`/`ts_to`,
  `RequestContext.as_of_range`.
- `b911496` part 3 — 12 new tests in `test_round6.py` covering
  the new behaviors; revised TC-008 + new TC-008b for the warning
  movement; OpenAPI updates for the new schema fields; close-out.

**Phase 2g re-validation on a fresh shadow** with the BLOCKER fix:
9 registry rows, 9 triggers, 9 PIT functions, 16 feature flags,
**10/10 invariants PASS**.

**Cumulative test count:** 48 collected under `pytest -m
integration` (was 35). Lint (ruff) + types (mypy --strict) clean
across every file in scope.

| Severity | Codex flagged | Closed | Remaining |
|---|---|---|---|
| BLOCKER | 1 | 1 | 0 |
| HIGH | 4 | 4 | 0 |
| MEDIUM | 6 | 6 | 0 |
| LOW | 6 | 6 | 0 |

The remaining "deferred" items (cursor keyset-WHERE resumption per
D7; `doc.filing_at` SCD-4 mirror) are deliberate v1.0.0-beta scope
decisions, not bugs.

## What's truly remaining

Operator/external actions only — not bypassable autonomously per
spec:

1. **Apply migrations to staging via `alembic upgrade head`.**
   STATE.md notes that "staging IS prod" today (no separate
   staging DB). Running `alembic upgrade head` against `aslan` on
   Hetzner is effectively a production migration; gated on
   `PROMOTE_TO_PROD`.
2. **Place `PROMOTE_TO_PROD` file at workspace root.** Explicit
   human gate per spec ("Production deploy is sidar's decision").
3. **Promote ADR-001/002/003 from PROVISIONAL to FINAL.** The
   stubs were synthesized from existing CLAUDE.md content and are
   internally consistent; flipping the status is a one-line edit
   per file when sidar has read them.
4. **Phase 7 production deploy.** Gated on (1) and (2).

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
