# Phase 1 — Decision diff (H8)

This document classifies every Phase 1 decision (D1–D30) by
reversibility and pins each to a feature flag (per H3) where the
decision is reversible MEDIUM or LOW. Sidar visibility callouts
appear at the top.

**Drafted:** 2026-05-09 by agent during autonomous build.

---

## "Review when convenient" — sidar callouts

These decisions are **most worth your eye** before staging/production
flag flips. None of them block Phase 2 work, but each is one
sidar-tweak away from adoption and they have the longest-tail
consequences if wrong.

1. **D3 / D14 — Pre-bitemporal kap.disclosures handling.** The
   chosen path is `as_of = COALESCE(body_fetched_at, index_fetched_at,
   published_at)` with NULL when none, surfaced via FF
   `BITEMPORAL_API_ALLOW_NULL_AS_OF`. Alternatives considered:
   sentinel timestamps (rejected per ADR-002), exclude-from-API
   (rejected — 372k rows is too many to drop). If you'd prefer
   `published_at` always (simpler, less honest), it's a one-flag flip.

2. **D8 — TAS 29 restatement chain.** Chosen: new `as_of` row with
   `restatement_kind` tag in `ts.canonical_financial`. Alternative:
   separate `ts.canonical_financial_restated` table. The chosen path
   keeps the moat-2 contract clean but adds a column to a 1.7M row
   table. Phase 2 migration is reversible (column-add, no UPDATE) but
   the data shape is then committed.

3. **D11 — kap.disclosures append-only trigger.** Phase 2 work
   coordinated with `crawl` (the `body_fetched=true` UPDATE path
   today). The trigger replaces the in-place mutation with a new-row
   pattern. Reversible at trigger level, but the new-row pattern
   changes the body-fetcher semantics in `crawl`. Surface in the
   crawl-side PR review.

4. **D18 — Audit log retention 13 months.** Matches
   `aslan-event-extractor/SCOPE.md` §6 GDPR floor. Could be longer
   (some industries demand 7 years). Cheap to extend; expensive to
   shorten retroactively (data already deleted).

5. **D27 — Single-service deployment.** Chosen: router in existing
   `aslan-dashboard-api`, not a separate service. If you'd prefer
   isolation upfront, switching is reversible MEDIUM (deploy/infra
   change, no client visibility).

6. **D30 — Default-redacted PII.** Chosen: redacted by default;
   per-key `pii_unredacted` scope unlocks. Conservative, GDPR-
   defensible. If a customer needs unredacted by default for a use
   case (e.g., regulatory reporting), separate API key with
   audit-trailed access is the expected path.

The other 24 decisions are either HIGH-reversibility (low-cost to
change) or are derivative of already-blessed schema (e.g., D9 uses
existing `ref.identifier` discipline).

---

## Reversibility classification (full)

### LOW reversibility (9 decisions)

These reshape data, schema, or compliance posture in ways that are
costly to undo. **Per H3, all must be PROVISIONAL with a feature flag,
unless the underlying schema decision predates this spec** (D9 is the
only exception — existing `ref.identifier`).

| ID | Topic | Status | Feature flag | Default |
|---|---|---|---|---|
| D3 | Pre-bitemporal kap.disclosures handling | PROVISIONAL | `BITEMPORAL_API_ALLOW_NULL_AS_OF` | `true` (staging); `true` (prod after canary green ≥1h) |
| D8 | TAS 29 restatement chain | PROVISIONAL | `BITEMPORAL_API_TAS29_RESTATEMENT_CHAIN` | `false` until Phase 2; `true` after Phase 2j gate |
| D9 | Identifier change handling | **FINAL** (exception — existing `ref.identifier` schema; no new decision in this spec) | — | — |
| D10 | Entity merge/split lineage | PROVISIONAL | `BITEMPORAL_API_ENTITY_MERGE_LINEAGE` | `false` until Phase 2 |
| D11 | KAP append-only trigger | PROVISIONAL | `BITEMPORAL_API_KAP_APPEND_ONLY_TRIGGER` | `false` until Phase 2c |
| D18 | Audit log table + 13mo retention | PROVISIONAL | `BITEMPORAL_API_AUDIT_LOG_ENABLED` | `true` |
| D23 | Path-based v1 versioning | PROVISIONAL | `BITEMPORAL_API_V2_PREVIEW` | `false` |
| D28 | bitemporal_table_registry CI enforcement | PROVISIONAL | `BITEMPORAL_TABLE_REGISTRY_CI_ENFORCED` | `true` |
| D30 | Default-redacted PII | PROVISIONAL | `BITEMPORAL_API_PII_EXPOSURE` | `false` |

### MEDIUM reversibility (9 decisions)

Data shape stable, but client/contract impact. Per H3, all must be
PROVISIONAL with a feature flag.

| ID | Topic | Status | Feature flag | Default |
|---|---|---|---|---|
| D2 | Query semantics (PIT/interval/current) | PROVISIONAL | `BITEMPORAL_API_INTERVAL_QUERIES` | `true` (staging); `true` (prod after canary) |
| D5 | API key auth | PROVISIONAL | `BITEMPORAL_API_AUTH_ADDITIONAL_SCHEMES` | empty |
| D6 | Three rate-limit tiers | PROVISIONAL | `BITEMPORAL_API_RATE_LIMIT_ENFORCED` | `true` |
| D7 | Cursor pagination | PROVISIONAL | `BITEMPORAL_API_CURSOR_VERSION` | `1` |
| D14 | NULL for unknown as_of | PROVISIONAL | `BITEMPORAL_API_ALLOW_NULL_AS_OF` (shared with D3) | `true` |
| D15 | Response envelope shape | PROVISIONAL | `BITEMPORAL_API_ENVELOPE_VERSION` | `1` |
| D16 | RFC 7807 error envelope | PROVISIONAL | `BITEMPORAL_API_ERROR_FORMAT_VERSION` | `1` |
| D19 | Query cost limits | PROVISIONAL | `BITEMPORAL_API_COST_LIMITS_ENFORCED` | `true` |
| D27 | Single-service deploy | PROVISIONAL | `BITEMPORAL_API_ENABLED` (master) | `false` (prod) / `true` (staging after Phase 5) |

### HIGH reversibility (12 decisions)

Code-only changes; no schema or data impact. FINAL is acceptable
without a feature flag.

| ID | Topic | Status |
|---|---|---|
| D1 | Tables/views exposed | FINAL |
| D4 | REST + OpenAPI 3.1 | FINAL |
| D12 | Microsecond as_of granularity | FINAL |
| D13 | UTC at storage; display_tz convenience | FINAL |
| D17 | Immutable cache for past, no-cache for current | FINAL |
| D20 | Public Moat 2 verification endpoint | FINAL |
| D21 | Swagger + ReDoc + OpenAPI JSON | FINAL |
| D22 | Python SDK first | FINAL |
| D24 | Deprecation+Sunset headers + 6 month policy | FINAL |
| D25 | Prometheus + OTel + structlog | FINAL |
| D26 | New router additive on existing FastAPI | FINAL |
| D29 | Multi-tier test strategy | FINAL |

---

## Feature flag inventory cross-check (H3 gate)

| Flag | Tied decisions | Default (staging / prod) |
|---|---|---|
| `BITEMPORAL_API_ENABLED` (master) | D27 | `true` (after Phase 5g) / `false` (until Phase 7e) |
| `BITEMPORAL_API_INTERVAL_QUERIES` | D2 | `true` / `true` |
| `BITEMPORAL_API_ALLOW_NULL_AS_OF` | D3, D14 | `true` / `true` |
| `BITEMPORAL_API_AUTH_ADDITIONAL_SCHEMES` | D5 | empty / empty |
| `BITEMPORAL_API_RATE_LIMIT_ENFORCED` | D6 | `true` / `true` |
| `BITEMPORAL_API_CURSOR_VERSION` | D7 | `1` / `1` |
| `BITEMPORAL_API_TAS29_RESTATEMENT_CHAIN` | D8 | `true` (after Phase 2) / `true` (after Phase 7) |
| `BITEMPORAL_API_ENTITY_MERGE_LINEAGE` | D10 | `true` (after Phase 2) / `true` (after Phase 7) |
| `BITEMPORAL_API_KAP_APPEND_ONLY_TRIGGER` | D11 | `true` (after Phase 2) / `true` (after Phase 7) |
| `BITEMPORAL_API_ENVELOPE_VERSION` | D15 | `1` / `1` |
| `BITEMPORAL_API_ERROR_FORMAT_VERSION` | D16 | `1` / `1` |
| `BITEMPORAL_API_AUDIT_LOG_ENABLED` | D18 | `true` / `true` |
| `BITEMPORAL_API_COST_LIMITS_ENFORCED` | D19 | `true` / `true` |
| `BITEMPORAL_API_V2_PREVIEW` | D23 | `false` / `false` |
| `BITEMPORAL_TABLE_REGISTRY_CI_ENFORCED` | D28 | `true` / `true` |
| `BITEMPORAL_API_PII_EXPOSURE` | D30 | `false` / `false` |

**16 flags total.** All MEDIUM/LOW PROVISIONAL decisions have a flag.
D9 is the documented exception (LOW reversibility, FINAL status,
no new decision made in this spec).

---

## Status counts

- FINAL: 13 (D1, D4, D9, D12, D13, D17, D20, D21, D22, D24, D25, D26, D29)
- PROVISIONAL: 17 (all MEDIUM/LOW reversibility decisions where the
  scope is in this spec)

## Reversibility counts

- HIGH: 12
- MEDIUM: 9
- LOW: 9

## Phase 1 gate readiness check

| Criterion | Status |
|---|---|
| All decisions D1–D30 present in DECISIONS_LOG.json | ✓ |
| All MEDIUM/LOW reversibility decisions have feature flags (or documented exception) | ✓ (D9 documented exception) |
| OPENAPI.yaml validates against OpenAPI 3.1 spec | pending — Phase 1d agent in flight |
| TESTPLAN.md has ≥50 test cases | pending — Phase 1e agent in flight |
| PHASE_1_DECISION_DIFF.md exists | ✓ (this file) |

---

## Carry-forward to Phase 2

Phase 2 design review (Phase 2a–2c) must explicitly:

1. Address `agg.filing_event.superseded_at` UPDATE pattern under the
   new append-only trigger discipline (per
   EXISTING_PATTERNS_AUDIT.md cross-cutting findings).
2. Decide whether `ref.entity_relationship` and `ref.entity_sector`
   convert to daterange-EXCLUDE (matching `ref.identifier`) or to
   `as_of`-PK (matching `ts.observation`). Defer to Phase 2 design
   review meeting.
3. Coordinate the `kap.disclosures` `body_fetched=true` UPDATE
   pattern transition to a new-row pattern with the `crawl` repo
   maintainers (a CRAWL_PATCHES/ migration patch will be authored
   for review).
