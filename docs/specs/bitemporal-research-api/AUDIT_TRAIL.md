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
