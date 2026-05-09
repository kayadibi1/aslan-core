10 findings; 0 BLOCKER, 3 HIGH, 5 MEDIUM, 2 LOW, 0 NIT

## HIGH

### Section 4 / X1

- **[HIGH]** `src/aslan_core/api/routes/research.py:503` — `as_of_range` is accepted without checking the `BITEMPORAL_API_INTERVAL_QUERIES` feature flag.
  - **Why it's a bug:** SCOPE D2 and TESTPLAN TC-074 require `BITEMPORAL_API_INTERVAL_QUERIES=false` to reject interval queries while leaving PIT queries available. Round 6 added `as_of_range` to every list endpoint, but no route checks that flag; `rg BITEMPORAL_API_INTERVAL_QUERIES src/aslan_core/api` returns no implementation use. If not fixed, the interval path cannot be safely disabled after a production regression.
  - **Repro / verification:** Static: `rg -n "BITEMPORAL_API_INTERVAL_QUERIES|as_of_range" src/aslan_core/api/routes/research.py docs/specs/bitemporal-research-api/SCOPE.md docs/specs/bitemporal-research-api/TESTPLAN.md` shows the flag in the contract and only unconditional parsing in the route.
  - **Suggested fix:** Add a shared interval gate after `_reject_pit_with_interval`: if `as_of_range is not None` and `flags.enabled.get("BITEMPORAL_API_INTERVAL_QUERIES", False)` is false, raise `HTTPException(503)` with code `FEATURE_DISABLED` and `extensions.flag`.
  - **Section:** 4 / X1

- **[HIGH]** `src/aslan_core/api/routes/research.py:1298` — The disclosure API still cannot serve D3 `include_pre_bitemporal=true` rows.
  - **Why it's a bug:** SCOPE D3 and OpenAPI say `/disclosures` and `/disclosures/{disclosure_id}` accept `include_pre_bitemporal=true` to include `as_of=NULL` rows with `pre_bitemporal=true`. The route signatures have no `include_pre_bitemporal` parameter, and both disclosure paths read through `kap.disclosures_at(:as_of)`, whose SQL filters `v.as_of <= p_as_of`, excluding NULL `as_of` rows. If not fixed, the documented ~372k pre-bitemporal disclosure rows remain unreachable through the public contract.
  - **Repro / verification:** Static: `rg -n "include_pre_bitemporal|kap.disclosures_at\\(:as_of\\)|WHERE v.as_of <= p_as_of" docs/specs/bitemporal-research-api/SCOPE.md docs/specs/bitemporal-research-api/OPENAPI.yaml src/aslan_core/api/routes/research.py src/aslan_core/db/migrations/versions/20260509_0708_0051_kap_disclosures_bitemporal.py`.
  - **Suggested fix:** Add `include_pre_bitemporal: bool = Query(False)` to both disclosure routes and branch to include provenance rows explicitly, or update SCOPE/OpenAPI to remove the D3 include path if it is intentionally deferred.
  - **Section:** 4 / X2 / X5

- **[HIGH]** `src/aslan_core/api/routes/research.py:763` — Interval-mode version-chain queries do not order rows by `as_of` ascending.
  - **Why it's a bug:** TESTPLAN TC-014 requires `as_of_range` responses to return the full version chain ordered by `as_of` ascending. Round 6 reuses PIT display ordering for interval mode (`period_end DESC`, `legal_name`, `published_at DESC`, `event_ts DESC`, etc.), so version chains can be returned out of temporal order even when all rows are present. If not fixed, amendment/replay clients can process a chain in the wrong sequence.
  - **Repro / verification:** Static: `rg -n "order_by=|TC-014|as_of ascending" src/aslan_core/api/routes/research.py docs/specs/bitemporal-research-api/TESTPLAN.md` shows all list endpoints keep non-temporal ordering while TC-014 requires `as_of` ascending.
  - **Suggested fix:** When `as_of_range` is present, use interval-specific `ORDER BY` clauses that include the natural key plus `as_of ASC`; add a valid SQL-path test that seeds multiple versions and asserts order.
  - **Section:** 4 / X1

## MEDIUM

### Section 4 / X1

- **[MEDIUM]** `src/aslan_core/api/routes/research.py:606` — Interval queries use `NOW()` as the cache basis instead of the interval upper bound.
  - **Why it's a bug:** SCOPE D17 says `as_of_range` requests are immutable when `T2 <= NOW() - 5 minutes`; otherwise they are `no-cache`. Every list route sets `resolved = requested or now_utc()` and passes that to `set_cache_headers`, so an old interval with no `as_of` is treated as current and gets `no-cache`. If not fixed, historical interval queries lose the immutable-cache behavior D17 made final.
  - **Repro / verification:** Static: `rg -n "set_cache_headers\\(response, as_of_resolved=resolved|as_of_range requests treated as immutable" src/aslan_core/api/routes/research.py docs/specs/bitemporal-research-api/SCOPE.md`.
  - **Suggested fix:** Compute `cache_as_of = interval[1] if interval is not None else resolved` for list routes and pass that to `set_cache_headers`.
  - **Section:** 4 / X1

- **[MEDIUM]** `src/aslan_core/api/routes/research.py:494` — `limit > 500` returns FastAPI validation 422 instead of the documented `QUERY_TOO_LARGE` 413.
  - **Why it's a bug:** SCOPE D19 and OpenAPI define exceeded cost caps as `QUERY_TOO_LARGE` with status 413. The route parameter uses `Query(le=500)`, so `limit=501` never reaches `_enforce_query_cost`; the new test explicitly asserts 422. If not fixed, clients get the wrong error code/envelope for page-size violations and the structured cost-cap path is unreachable.
  - **Repro / verification:** Static and command-backed: `rg -n "limit: int = Query\\(default=50, ge=1, le=500\\)|QUERY_TOO_LARGE|test_limit_above_500|assert resp.status_code == 422" src/aslan_core/api/routes/research.py tests/research/test_round6.py docs/specs/bitemporal-research-api/SCOPE.md`.
  - **Suggested fix:** Remove `le=500` from route parameters, keep `ge=1`, let `_enforce_query_cost` raise 413 `QUERY_TOO_LARGE`, and update the round-6 test to assert the documented contract.
  - **Section:** 4 / X2

- **[MEDIUM]** `src/aslan_core/api/routes/research.py:1300` — Several documented query filters are still ignored because route parameter names do not match OpenAPI.
  - **Why it's a bug:** OpenAPI documents `/disclosures` filters `kap_company_id`, `form_type`, `published_from`, and `published_to`, while the route only accepts `entity_id`, `published_after`, and `published_before`. `/filings` documents `source_kind`, `filed_from`, `filed_to`, while the route uses `received_after` / `received_before`. `/events` documents `filing_id`, `occurred_from`, `occurred_to`, while the route uses `event_after` / `event_before`. FastAPI ignores unknown query parameters by default, so documented filters silently do nothing. If not fixed, SDK/generated-client users can receive over-broad result sets while believing filters were applied.
  - **Repro / verification:** Static: `rg -n "kap_company_id|form_type|published_from|published_after|filed_from|received_after|occurred_from|event_after" docs/specs/bitemporal-research-api/OPENAPI.yaml src/aslan_core/api/routes/research.py`.
  - **Suggested fix:** Rename route parameters to the OpenAPI names and apply the missing filters, or update OpenAPI/README/SDK docs to the implemented names and reject unknown filter names explicitly.
  - **Section:** 4 / X2

- **[MEDIUM]** `src/aslan_core/api/routes/research.py:1092` — Entity interval responses attach lineage from the request's resolved `NOW()` instead of each version row's `as_of`.
  - **Why it's a bug:** In interval mode, `/entities` reads rows from `ref.entity_version`, but then calls `ref.entity_lineage_at(:as_of)` with `as_of=resolved`, which is `NOW()` when no PIT `as_of` was supplied. A historical version row can therefore carry future merge/split lineage. If not fixed, interval replay of entity history can show lineage events that did not exist at the row's own `as_of`.
  - **Repro / verification:** Static: `rg -n "FROM ref.entity_lineage_at\\(:as_of\\)|\\{\"as_of\": resolved|as_of_range=interval" src/aslan_core/api/routes/research.py`.
  - **Suggested fix:** In interval mode, bind lineage lookup to `r.as_of` for each version row, or query lineage events in the same `[T1,T2)` interval and attach only events visible at that version.
  - **Section:** 4 / X1

### Section 6

- **[MEDIUM]** `docs/specs/bitemporal-research-api/TESTPLAN.md:318` — The new round-6 tests still do not exercise a valid interval SQL path.
  - **Why it's a bug:** Round 6 added parser and negative-route tests, but TC-014 through TC-019 remain unreferenced by collected tests. The valid `as_of_range` path that rewrites PIT calls to physical history tables is not data-seeded in tests, which leaves the ordering, filter, and SCD-4 version-chain bugs above uncovered. If not fixed, CI can pass while interval mode is contract-incomplete.
  - **Repro / verification:** Command-backed check: `pytest tests/research/ --collect-only -m integration -q` collected 48 tests; static TC extraction found 33 referenced TC IDs versus 76 plan IDs, with TC-014, TC-015, TC-016, TC-017, TC-018, and TC-019 still uncovered.
  - **Suggested fix:** Add integration tests that seed multiple `as_of` versions for at least `ts.canonical_financial`, `ref.entity_version`, and `kap.disclosures_version`, then assert row inclusion, half-open boundaries, ordering, pagination metadata, and warnings.
  - **Section:** 6

## LOW

### Section 4 / X2

- **[LOW]** `src/aslan_core/api/routes/research.py:108` — The `as_of_range` parser accepts bracket forms that OpenAPI rejects.
  - **Why it's a bug:** OpenAPI's `AsOfRange` pattern allows only the canonical half-open `[T1,T2)` form, but `_parse_as_of_range` accepts `[T1,T2]`, `(T1,T2)`, and `(T1,T2]` and normalizes them by microsecond adjustment. If not fixed, generated clients reject inputs the server accepts, and server tests can bless non-contract syntax.
  - **Repro / verification:** Static: `rg -n "accepts shapes|if close_bracket|pattern: '\\^\\\\\\['" src/aslan_core/api/routes/research.py docs/specs/bitemporal-research-api/OPENAPI.yaml`.
  - **Suggested fix:** Either restrict the parser to `[T1,T2)` or update OpenAPI/SCOPE to document all accepted bracket forms and their microsecond normalization.
  - **Section:** 4 / X2

### Section 5 / X3

- **[LOW]** `src/aslan_core/api/research_logging.py:177` — The research exception-handler boundary still uses an unbounded prefix match.
  - **Why it's a bug:** `_is_research_path` returns true for any path starting with `/v1/research`, including future non-research paths like `/v1/research-status`. Pass 1 fixed the same class in auth and audit middleware, but the logging/error-envelope boundary still has it. If not fixed, future adjacent routes can receive research RFC7807/logging behavior unexpectedly.
  - **Repro / verification:** Static: `rg -n "def _is_research_path|startswith\\(_RESEARCH_PREFIX" src/aslan_core/api/research_logging.py`.
  - **Suggested fix:** Match `path == "/v1/research"` or `path.startswith("/v1/research/")`, mirroring `research_audit_middleware`.
  - **Section:** 5 / X3

## Run Summary / Deferred Checks

- Pass-1 closure verification: the prior `ref.identifier` daterange bug is fixed; disclosure IDs are now `str`; `ref.entity_at` and `kap.disclosures_at` are used for PIT disclosure/entity reads; response-shape keys were added; `_PUBLIC_PATHS` is exact-match; research exception handlers now win; canary command uses escaped shell env vars on executable lines; CI path scope now includes the research support modules and migrations.
- Command-backed checks completed: D1-D30 coverage passed; reversibility/feature-flag inventory passed; OpenAPI path count matched router path count at 15/15; `openapi-spec-validator` passed; ruff passed on the research API surface and scripts; mypy `--strict` passed after adding transient `types-PyYAML`; `pytest --collect-only -m integration tests/research/` collected 48 tests; boot smoke registered 15 `/v1/research` routes and confirmed the research `StarletteHTTPException` handler is active; `KNOWN_AMENDMENTS` count is 10.
- Live Postgres / Hetzner checks remain unavailable in this sandbox: check requires live Postgres; deferred. Full trigger execution tests against Docker/testcontainers were not run; static analysis and collection were used for this pass.
