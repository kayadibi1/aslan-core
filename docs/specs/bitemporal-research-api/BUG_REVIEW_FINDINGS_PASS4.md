3 findings; 0 BLOCKER, 0 HIGH, 3 MEDIUM, 0 LOW, 0 NIT

## BLOCKER

None.

## HIGH

None.

## MEDIUM

### Section 1 / X1

- **[MEDIUM]** `docs/specs/bitemporal-research-api/SCOPE.md:906` - The retired `BITEMPORAL_API_ALLOW_NULL_AS_OF` flag is still listed as an active feature flag and still seeded by migration 0048.
  - **Why it's a bug:** SCOPE D3 now says `BITEMPORAL_API_ALLOW_NULL_AS_OF` is retired because NULL `as_of` behavior is unreachable, but the feature-flag inventory still gives it staging/prod defaults, OpenAPI `x-feature-flags` still advertises it, and migration 0048 still inserts it with `value_bool=true`. If not fixed, operators and generated docs will treat a dead flag as an active control plane knob, and env audits will disagree about whether D3 is governed by a live flag.
  - **Repro / verification:** Static: `rg -n "BITEMPORAL_API_ALLOW_NULL_AS_OF|Feature flag inventory|retired" docs/specs/bitemporal-research-api/SCOPE.md docs/specs/bitemporal-research-api/OPENAPI.yaml src/aslan_core/db/migrations/versions/20260509_0705_0048_research_api_support_tables.py` shows D3 retired at SCOPE line 177, but inventory line 906, OpenAPI line 42, and migration line 123 still model it as active.
  - **Suggested fix:** Remove the flag from SCOPE section 3 and OpenAPI `x-feature-flags`, and either remove it from the seed migration before release or add a follow-up migration that deletes/marks the seed row retired; if the row must remain for upgrade compatibility, document it consistently as a no-op retired flag everywhere.
  - **Section:** 1 / X1

- **[MEDIUM]** `docs/specs/bitemporal-research-api/OPENAPI.yaml:811` - OpenAPI still documents D3 as NULL-`as_of` disclosure behavior after SCOPE D3 was finalized to non-null proxy timestamps.
  - **Why it's a bug:** SCOPE D3 says the implemented contract is a non-null proxy timestamp with `as_of_provenance`, and migration 0051 creates `kap.disclosures_version.as_of TIMESTAMPTZ NOT NULL`; OpenAPI still says about 372k rows have `as_of=NULL`, says `include_pre_bitemporal=true` includes NULL rows, and describes `pre_bitemporal_unknown` as the marker for NULL `as_of`. If not fixed, clients will code against impossible response semantics and support staff will debug "missing NULL rows" that the schema can never emit.
  - **Repro / verification:** Static: `rg -n "as_of=NULL|NULL-as_of|pre_bitemporal_unknown|TIMESTAMPTZ NOT NULL|proxy timestamp" docs/specs/bitemporal-research-api/OPENAPI.yaml docs/specs/bitemporal-research-api/SCOPE.md src/aslan_core/db/migrations/versions/20260509_0708_0051_kap_disclosures_bitemporal.py` shows stale OpenAPI text at lines 811, 813, 2002, and 2351 against the non-null D3/migration contract.
  - **Suggested fix:** Rewrite the disclosure endpoint description, warning example, and Disclosure schema description to state that current prod backfill rows receive non-null proxy `as_of` values and that `pre_bitemporal_unknown` is reserved for future schemas where the source timestamp can truly be unknown.
  - **Section:** 1 / X1

### Section 4 / X2

- **[MEDIUM]** `docs/specs/bitemporal-research-api/OPENAPI.yaml:295` - Newly wired `/financials/line-items` filters still disagree with the route and table contract.
  - **Why it's a bug:** Round 8 added route filters for `filing_id`, `statement_type`, `line_code`, `consolidation`, and exact `period_end`, but the OpenAPI contract still documents `filing_id` as a KAP string and `statement_type` as `[balance_sheet, income_statement, cash_flow, equity_changes]`. The route requires `filing_id: UUID` and `statement_type` matching `^(bs|is|cf|eq|notes)$`, which matches `ts.financial_line_item` migration 0029. If not fixed, a documented request such as `?filing_id=KAP-2024-1234567&statement_type=income_statement` returns a 422 instead of filtering.
  - **Repro / verification:** Static: `rg -n "filing_id|statement_type|financial_line_item" docs/specs/bitemporal-research-api/OPENAPI.yaml src/aslan_core/api/routes/research.py src/aslan_core/db/migrations/versions/20260502_0004_0029_ts_financial_line_item.py` shows OpenAPI lines 295-305 and schema lines 2167-2173 use the old public strings, while `routes/research.py:935` and migration 0029 require UUID plus short statement codes.
  - **Suggested fix:** Update the line-item query parameter and `FinancialLineItem` schema to use `format: uuid` for `filing_id` and enum `[bs, is, cf, eq, notes]` for `statement_type`, or change the route to accept documented public aliases and translate them before SQL.
  - **Section:** 4 / X2

## LOW

None.

## NIT

None.

## Pass-3 Closure Verification

- Finding 1 closed: `_gate_interval_flag` now defaults missing `BITEMPORAL_API_INTERVAL_QUERIES` to true while explicit `value_bool=false` still returns 503.
- Finding 2 closed by contract change: SCOPE D3 now describes the implemented non-null proxy timestamp + `as_of_provenance` model and marks the NULL-as-of flag retired.
- Finding 3 closed: `/disclosures` rejects `include_pre_bitemporal=true` with `as_of_range` using 400 `BITEMPORAL_INTERVAL_INVALID`.
- Finding 4 closed: `/quality-scores` OpenAPI now includes the `AsOfRange` parameter.
- Finding 5 closed functionally: `/financials/line-items` forwards all newly added filters into SQL and includes them in `filters_for_cursor`; the remaining issue is the OpenAPI value contract above.
- Finding 6 closed: round-7 interval test cleanup now deletes `ref.entity` before deleting `ref.entity_version`, so the delete trigger's emitted version row is cleaned up.

## Run Summary / Deferred Checks

- Command-backed checks completed: D1-D30 coverage passed; OpenAPI path count matched router path count at 15/15; `openapi-spec-validator` passed; ruff passed on the research API surface, scripts, and round-7 tests; mypy `--strict` passed on the research API modules; `pytest --collect-only -m integration tests/research/` collected 56 tests; boot smoke registered 15 `/v1/research` routes and confirmed the research `StarletteHTTPException` handler is active; `KNOWN_AMENDMENTS` count is 10.
- Static closure checks completed for the pass-4 focus areas: every list endpoint calls `_gate_interval_flag`; `include_pre_bitemporal` rejection is PIT-only on disclosures as intended; interval ordering uses physical natural keys; line-item filters are forwarded to SQL and cursor filters; test cleanup order matches the entity delete trigger.
- Live Postgres / Hetzner checks remain unavailable in this sandbox: check requires live Postgres; deferred. Full trigger execution tests against Docker/testcontainers were not run; static analysis and collection were used for this pass.
