# Bitemporal Research API — Implementation notes

This file accumulates notes about divergences between the
`bitemporal-api-prompt.md` brief and the actual implementation. It
lives outside the formal SCOPE.md so the SCOPE.md stays clean for
sidar review.

## Coordination protocol (H4)

**Brief said:** Use `pg_try_advisory_lock(...)` for global agent
exclusion; route around held resources by re-acquiring per-resource
locks.

**Implementation:** Postgres advisory locks are session-scoped, and
Claude Code cannot hold a long-lived psql session within a single
tool call. Therefore:

1. **Inter-agent claim** is the file
   `docs/coordination/bitemporal-api-<ISO8601>.lock`. Atomic via
   filesystem rename. Visible to all agents that read
   `docs/coordination/`. Heartbeat updated every ≤15 minutes of
   active work.
2. **Audit-style probe** of the global advisory lock is recorded in
   `AUDIT_TRAIL.md` for the record.
3. **Per-migration atomicity** uses `pg_advisory_xact_lock(...)`
   *inside* the migration transaction, where the lock auto-releases
   on COMMIT/ROLLBACK and cannot leak.

If sidar prefers true session-scoped advisory locks, implement via
`ssh hetzner 'docker exec -i ... psql ...'` started with
`run_in_background` and held for the agent's lifetime. Not done in
v1.

## Database environment

**Brief said:** Connect to "the workspace Postgres."

**Implementation:** No local Postgres on the Windows agent host.
The platform DB lives on Hetzner (`hetzner` SSH alias →
`aslan-dashboard-postgres-1` Docker container, db `aslan`, owner
`aslan`, Postgres 16 with TimescaleDB). All Phase 2+ DB work runs
via SSH-piped psql or via a forwarded tunnel.

The brief's `createdb shadow_db` step is implemented as
`docker exec aslan-dashboard-postgres-1 createdb -U aslan
aslan_shadow_<epoch>` on Hetzner, owned by `aslan`.

## ADRs

**Brief said:** Read `docs/decisions/ADR-001/002/003`.

**Implementation:** No ADRs existed on disk before the build.
Per sidar's direction (asked at session start), the agent
synthesized PROVISIONAL stubs from:

- workspace `CLAUDE.md` §1-§4 (mission, decision filter, cost,
  Bloomberg moats);
- `crawl/CLAUDE.md` (Turkish handling, raw store, no auto-translate);
- `aslan-event-extractor/CLAUDE.md` (Bloomberg-bar operational, cost
  discipline);
- `aslan-event-extractor/SCOPE.md` D14, D16, D17, D18 (LLM model
  selection, batch, prompt caching, taxonomy).

The stubs live at `docs/decisions/ADR-00[123]-*.md` and are marked
PROVISIONAL. The bitemporal-research-API SCOPE.md treats them as
binding for the build but flags them as awaiting sidar's
canonicalization.

## Runtime cap (H12)

**Brief said:** 24h runtime cap, 5M token cap, clean exit on cap.

**Implementation:** Claude Code is interactive — there is no
24-hour autonomous runtime. A session ends when the conversation
ends. The agent treats the prompt's caps as a *protocol intent*:
write `HANDOFF.md`, update lock to `partial`, exit cleanly when
the session ends, regardless of phase completion.

## Concurrent agents (H4)

**Brief said:** Two other Claude Code instances are running.

**Observed:** `docs/coordination/` was empty at start; no
`bitemporal|research` branches in `aslan-core`. Sidar asserted at
session start that other agents are running. The agent followed the
coordination protocol regardless (file lock, advisory probe), which
costs nothing if no one else is active and provides safety if they
are.

## Production gate (H7)

**Brief said:** Phase 7 gated on `PROMOTE_TO_PROD` file at workspace
root.

**Implementation:** Honored. Phase 6 ends with `HANDOFF.md` and
draft PR. Phase 7 only runs if `PROMOTE_TO_PROD` is present at
session start of a subsequent run (the agent does not create it
itself; sidar must place it).

## Schemas with no aslan-core ownership

The `kap.*` schema is owned by the `crawl` repo's alembic. Phase 2
migrations that touch `kap.*` for bitemporal enforcement (triggers,
uniqueness on `(disclosure_id, as_of)`) cannot be authored as
aslan-core revisions; they must be authored as crawl revisions and
coordinated via PR. The agent will write them as patch files in
`aslan-core/docs/specs/bitemporal-research-api/CRAWL_PATCHES/` and
flag them in HANDOFF.md for crawl-side merge.
