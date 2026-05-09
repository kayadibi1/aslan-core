# Bitemporal Research API — chronological audit trail (H11)

This file is append-only. Every phase appends an entry. Entries are
ordered oldest-to-newest.

---

## 2026-05-09T05:54:35Z — Phase 0: setup

- **Phase:** Setup (pre-Phase-1).
- **Files created:**
  - `docs/coordination/bitemporal-api-2026-05-09T05-54-35Z.lock`
  - `docs/decisions/ADR-001-batch-first-llm.md` (PROVISIONAL stub)
  - `docs/decisions/ADR-002-language-policy.md` (PROVISIONAL stub)
  - `docs/decisions/ADR-003-bloomberg-bar.md` (PROVISIONAL stub; Moat 2 specified)
  - `aslan-core/docs/specs/bitemporal-research-api/AUDIT_TRAIL.md` (this file)
- **Branch created:** `aslan-core/feature/bitemporal-research-api` (from `main` at `09f5dcf`).
- **Coordination:** `pg_try_advisory_lock(hashtextextended('aslan-bitemporal-api-build',0))` returned `t` against Hetzner `aslan-dashboard-postgres-1`/`aslan` at `2026-05-09 05:58:06.084393+00` (PID 1573857; transient — released on session end). File-based lock in `docs/coordination/` is the durable claim.
- **Hetzner state observed:** Postgres 16, db `aslan`, container `aslan-dashboard-postgres-1` reachable via SSH alias `hetzner`.
- **ADR provenance:** ADR-001/002/003 synthesized from workspace `CLAUDE.md` §1-§4, `crawl/CLAUDE.md`, `aslan-event-extractor/CLAUDE.md`, and `aslan-event-extractor/SCOPE.md`. All three marked PROVISIONAL pending sidar review. The bitemporal-api-prompt referenced these as if extant; they did not exist on disk.
- **Constraint divergence noted:** The H4 protocol in the prompt assumes session-scoped `pg_try_advisory_lock`. Claude Code cannot hold a long-lived psql session; per-resource atomicity will be done via `pg_advisory_xact_lock` inside each migration transaction. Documented in the lock file.
- **Concurrent agents:** None observed in `docs/coordination/` (empty), no `bitemporal|research` branches in `aslan-core/git branch -a`. User asserted other agents are running; I have honored the protocol regardless.
- **Summary:** Foundation laid for Phase 1 work. Phase 1a (RESEARCH.md) and Phase 1b (EXISTING_PATTERNS_AUDIT.md) dispatched as background subagents.

---

## 2026-05-09T06:30:00Z — Phase 1 close

- **Phase:** Phase 1 (research, design).
- **Deliverables landed:**
  - `RESEARCH.md` (3,897 words, 63 sources cited; written by Phase 1a subagent).
  - `EXISTING_PATTERNS_AUDIT.md` (37 tables, 7 schemas; class A=4, C=1, D=13, E=1, F=14, G=1; written from Phase 1b subagent's read-only audit).
  - `SCOPE.md` (D1–D30 binding decisions, feature-flag inventory, schema impact, acceptance gate).
  - `OPENAPI.yaml` (2,705 lines, 14 endpoints, 28 schemas, 9 reusable error responses, 7 reusable parameters; validator-passed via `openapi-spec-validator`).
  - `TESTPLAN.md` (1,470 lines, 76 test cases across 16 categories).
  - `DECISIONS_LOG.json` (D1–D30; all MEDIUM/LOW PROVISIONAL behind feature flags except D9 documented exception).
  - `PHASE_1_DECISION_DIFF.md` (12 HIGH, 9 MEDIUM, 9 LOW reversibility; sidar callouts).
- **Phase 1 gate:** PASS (all five criteria met).
- **Commit:** `2879f45` on `aslan-core/feature/bitemporal-research-api`.

---

## 2026-05-09T07:25:00Z — Phase 2 close (partial; shadow-validated)

- **Phase:** Phase 2 (schema + invariants).
- **Concurrent-agent activity observed:** while this agent was working,
  a sibling agent merged `feature/m0-review-route` → `main` as commit
  `fa9531f`. No conflict on this build's files.
- **Deliverables landed:**
  - `scripts/check_bitemporal_invariants.py` (H2; Python with psycopg) — 10 invariants.
  - `scripts/check_bitemporal_invariants.sql` (pure-SQL fallback for SSH-piped runs).
  - 8 alembic migrations (0043–0050):
    - `0043_aslan_core_bitemporal_registry.py` — schema, registry table, generic trigger function.
    - `0044_append_only_triggers_class_a.py` — triggers + registry rows on 5 Class A tables.
    - `0045_canonical_financial_restatement_kind.py` — **NO-OP** (existing `restatement_basis` column already covers TAS 29).
    - `0046_ref_entity_bitemporal_upgrade.py` — **NO-OP / DEFERRED** (PK change cascades through 14+ FKs).
    - `0047_ref_entity_lineage.py` — bitemporal table for merge/split events.
    - `0048_research_api_support_tables.py` — `api_key`, `api_query_audit`, `api_rate_limit_state`, `feature_flags` (16 seeded flags).
    - `0049_pit_functions.py` — 6 PIT SQL functions.
    - `0050_entity_quality_score_bitemporal.py` — discovered Phase 2g; 7th Class A table.
  - `PHASE_2_SHADOW_VALIDATION.sql` — consolidated SQL applied to shadow.
- **Phase 2g shadow validation** (Hetzner shadow `aslan_shadow_1778293939` cloned from prod):
  - Schema dump from prod (11,582 lines, 367KB) applied cleanly.
  - All migration upgrade bodies applied without error after 3 iterations of correction.
  - Final shadow state: **7 registry rows, 7 triggers, 7 PIT functions, 16 feature flags**.
  - `check_bitemporal_invariants.sql` returns **10/10 PASS**.
- **Phase 2h reversibility:** logical reversibility verified per-migration (every `upgrade()` has a symmetric `downgrade()`; non-no-op migrations DROP what they CREATE). Full apply-down-up cycle on shadow not run in this session.
- **SCOPE.md amendments discovered during Phase 2g:**
  1. **D8 (TAS 29):** `ts.canonical_financial.restatement_basis` already exists with `('as_reported', 'cpi_normalized')` in the PK. No `restatement_kind` column needed.
  2. **D10 (entity merge/split):** `ref.entity` bitemporal upgrade deferred — PK change cascades through 14+ FKs.
  3. **EXISTING_PATTERNS_AUDIT.md miss:** `ts.entity_quality_score` is a 7th Class A bitemporal table.
  4. **`ref.identifier` PIT semantics:** uses date-typed `valid_from`/`valid_to` plus a daterange-EXCLUDE constraint. PIT uses `daterange(valid_from, valid_to, '[)') @> p_as_of::date`.
- **agg.filing_event coordination:** the `superseded_at` UPDATE pattern in `aslan-event-extractor/SCOPE.md` D6 is incompatible with the append-only trigger. Since `agg.filing_event` is currently empty, the discipline is enforced from day 0.
- **CRAWL_PATCHES not authored in this session:** `kap.disclosures` bitemporal upgrade (D11) deferred; flagged in HANDOFF.md.
