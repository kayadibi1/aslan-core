17 findings; 1 BLOCKER, 4 HIGH, 6 MEDIUM, 6 LOW, 0 NIT

## BLOCKER

### Section 3

- **[BLOCKER]** `scripts/check_bitemporal_invariants.py:184` — The H2 `ref.identifier` no-overlap invariant references `a.daterange` and `b.daterange`, but the table has `valid_from` / `valid_to` columns instead.
  - **Why it's a bug:** The live invariant gate will fail before it can validate class-H2 history. Migration `20260427_1612_0002_ref_schema_and_tables.py` defines `ref.identifier` with `valid_from` and `valid_to`, and the same script's SQL fallback already uses `daterange(a.valid_from, a.valid_to, '[)')`. If not fixed, Moat 2 / invariant validation cannot pass against the real schema.
  - **Repro / verification:** Static: `rg -n "a\\.daterange|valid_from|valid_to" scripts/check_bitemporal_invariants.py src/aslan_core/db/migrations/versions/20260427_1612_0002_ref_schema_and_tables.py` shows the bad query and the actual columns.
  - **Suggested fix:** Change the H2 query to compare `daterange(a.valid_from, a.valid_to, '[)') && daterange(b.valid_from, b.valid_to, '[)')`, or add a generated `daterange` column before referencing it.
  - **Section:** 3

## HIGH

### Section 4 / X1

- **[HIGH]** `src/aslan_core/api/routes/research.py:247` — The observations endpoint does not implement `as_of_range`, despite SCOPE and OpenAPI making interval PIT mode part of the contract.
  - **Why it's a bug:** SCOPE D2 marks interval time travel final, OpenAPI defines `AsOfRange`, and TESTPLAN has interval cases, but `routes/research.py` has no `as_of_range` reference. FastAPI will silently ignore `?as_of_range=...` and return a single PIT result, so clients can receive the wrong temporal answer without an error. If not fixed, interval queries and D19 cost-limit behavior remain contract-only.
  - **Repro / verification:** `rg -n "as_of_range|QUERY_TOO_LARGE" src/aslan_core/api/routes/research.py src/aslan_core/api` returns no route implementation; `docs/specs/bitemporal-research-api/SCOPE.md:117` and `docs/specs/bitemporal-research-api/OPENAPI.yaml:1513` define the feature.
  - **Suggested fix:** Add `as_of_range` parsing, validation, interval query paths, and D19 row/window caps that return `QUERY_TOO_LARGE`; otherwise remove the interval contract from SCOPE/OpenAPI/TESTPLAN.
  - **Section:** 4 / X1

- **[HIGH]** `src/aslan_core/api/routes/research.py:740` — Several PIT endpoints query current tables directly instead of the bitemporal `_at(:as_of)` functions.
  - **Why it's a bug:** `/entities`, `/entities/{id}`, `/disclosures`, `/disclosures/{id}`, and `/filings/{id}/events` read `ref.entity`, `kap.disclosures`, or `doc.filing` directly. SCOPE D1 binds REST to PIT functions, migration 0046 creates `ref.entity_at`, and migration 0051 creates `kap.disclosures_at`. If not fixed, historical `as_of` requests can return present-day rows or miss valid historical rows.
  - **Repro / verification:** `rg -n "FROM ref\\.entity|FROM kap\\.disclosures|FROM doc\\.filing|entity_at|disclosures_at" src/aslan_core/api/routes/research.py src/aslan_core/db/migrations/versions/20260509_0705_0046_ref_entity_bitemporal_upgrade.py src/aslan_core/db/migrations/versions/20260509_0902_0051_kap_disclosures_scd4.py`.
  - **Suggested fix:** Route entity and disclosure reads through `ref.entity_at(:as_of)` and `kap.disclosures_at(:as_of)`; add a `doc.filing_at(:as_of)` function or explicitly document filings as pre-bitemporal.
  - **Section:** 4 / X1

- **[HIGH]** `src/aslan_core/api/routes/research.py:973` — Disclosure routes select `republished_as`, while the SCD-4 migration and PIT function use `parent_disclosure_id`.
  - **Why it's a bug:** Migration 0051 creates `parent_disclosure_id` in `kap.disclosures_version`, carries it through `kap.disclosures_at`, and indexes it. The API queries `republished_as` from `kap.disclosures`, so either the API fails at runtime on the migrated schema or the migration/function no longer matches the live table. If not fixed, disclosure list/detail endpoints break or return incorrect republication lineage.
  - **Repro / verification:** `rg -n "republished_as|parent_disclosure_id" src/aslan_core/api/routes/research.py src/aslan_core/db/migrations/versions/20260509_0902_0051_kap_disclosures_scd4.py docs/specs/bitemporal-research-api/OPENAPI.yaml`.
  - **Suggested fix:** Standardize the physical column and PIT function on one name. If OpenAPI should expose `republished_as`, alias `parent_disclosure_id AS republished_as` at the API boundary.
  - **Section:** 2 / 4 / X5

- **[HIGH]** `src/aslan_core/api/routes/research.py:1038` — `disclosure_id` is typed as `UUID`, but KAP disclosure IDs are text identifiers in the migration and public contract.
  - **Why it's a bug:** Migration 0051 defines `disclosure_id TEXT`, and the docs/OpenAPI examples use IDs like `KAP-2024-...`. FastAPI rejects those IDs before the query runs. If not fixed, documented disclosure detail URLs cannot be called for real KAP IDs.
  - **Repro / verification:** `rg -n "disclosure_id: UUID|disclosure_id TEXT|KAP-" src/aslan_core/api/routes/research.py src/aslan_core/db/migrations/versions/20260509_0902_0051_kap_disclosures_scd4.py docs/specs/bitemporal-research-api/OPENAPI.yaml docs/specs/bitemporal-research-api/README.md`.
  - **Suggested fix:** Change the route parameter to `str`, validate only the documented KAP ID shape if needed, and update tests to cover a real `KAP-*` identifier.
  - **Section:** 4 / X2

## MEDIUM

### Section 1

- **[MEDIUM]** `docs/specs/bitemporal-research-api/TESTPLAN.md:318` — The test suite collects 35 research tests, but the TESTPLAN defines 76 cases and many contract-critical cases are uncovered.
  - **Why it's a bug:** Interval `as_of_range` tests, D19 query-size limits, several disclosure/entity drift cases, and negative envelope cases have no collected test IDs. If not fixed, unimplemented contract areas can regress or remain absent while CI still passes.
  - **Repro / verification:** Command-backed check: `pytest tests/research/ --collect-only -m integration -q` collected 35 tests after installing the required transient dependencies; static TC extraction found 33 unique TC IDs in `tests/research` versus 76 `TC-*` entries in TESTPLAN.
  - **Suggested fix:** Add tests for the missing high-risk TESTPLAN cases, starting with interval mode, `QUERY_TOO_LARGE`, OpenAPI response shape, and disclosure/entity PIT drift; or mark intentionally deferred cases outside the enforceable plan.
  - **Section:** 1 / 6

### Section 4 / X2

- **[MEDIUM]** `src/aslan_core/api/routes/research.py:248` — `/observations` accepts an optional integer `series_id`, while OpenAPI and README document a required string catalog ID such as `BIST.GARAN.close`.
  - **Why it's a bug:** A client following the docs sends `series_id=BIST.GARAN.close`, but the route type is `int | None`, so it is rejected with validation error. The route also exposes `obs_from` / `obs_to` instead of the documented `ts_from` / `ts_to`. If not fixed, the first documented quickstart request cannot work.
  - **Repro / verification:** `rg -n "series_id|BIST\\.GARAN|ts_from|obs_from" src/aslan_core/api/routes/research.py docs/specs/bitemporal-research-api/OPENAPI.yaml docs/specs/bitemporal-research-api/README.md`.
  - **Suggested fix:** Either resolve documented string series codes to numeric IDs at the API boundary and accept `ts_from` / `ts_to`, or update OpenAPI/README to the actual numeric parameter contract.
  - **Section:** 4 / X2

- **[MEDIUM]** `src/aslan_core/api/routes/research.py:768` — Entity, disclosure, and filing-event responses do not match their OpenAPI schemas.
  - **Why it's a bug:** Entity responses return `name`, `kind`, `created_at`, and `lineage_events`, while OpenAPI requires `canonical_name`, `country`, merge/split lineage fields, and `as_of`. Disclosure responses omit documented provenance/pre-bitemporal fields. Filing events return `filing_event_id`, `event_ts`, and `payload`, while OpenAPI requires `event_id`, `occurred_at`, and `attributes`. If not fixed, generated clients and schema validation will fail even when routes return 200.
  - **Repro / verification:** `rg -n "canonical_name|filing_event_id|occurred_at|pre_bitemporal|lineage_events" src/aslan_core/api/routes/research.py docs/specs/bitemporal-research-api/OPENAPI.yaml`.
  - **Suggested fix:** Align serializer keys with OpenAPI or regenerate OpenAPI from the implemented models; add response-shape tests for each public endpoint.
  - **Section:** 4 / X2

### Section 5 / X3

- **[MEDIUM]** `src/aslan_core/api/__init__.py:96` — Research exception handlers are registered before the generic handlers, so the generic `StarletteHTTPException` handler overwrites the research one.
  - **Why it's a bug:** FastAPI keeps one handler per exception class. The later generic registration replaces the research RFC7807/logging handler, so `HTTPException` paths lose the documented research error envelope and structured `research_http_exception` event. If not fixed, error responses and logs drift from D12/D24 and the logging coverage requirement.
  - **Repro / verification:** Runtime smoke: `create_api_app().exception_handlers[StarletteHTTPException].__qualname__` resolves to `register_exception_handlers.<locals>._http_exception_handler`, not `register_research_exception_handlers.<locals>._http_exception_handler`.
  - **Suggested fix:** Register generic handlers first and research handlers second, or merge the generic handler so research routes keep the RFC7807 envelope and structured logging.
  - **Section:** 5 / X3

### Section 8

- **[MEDIUM]** `infra/deploy/docker-compose.yml:314` — The canary loop uses `${BITEMPORAL_CANARY_INTERVAL_SECONDS}` in the shell command, so Compose interpolates it before the container starts.
  - **Why it's a bug:** The service environment sets `BITEMPORAL_CANARY_INTERVAL_SECONDS`, but the command is expanded by Compose from the host environment. When the host variable is absent, Compose substitutes blank and `sleep ""` terminates the loop under `set -e`. If not fixed, the canary may run once and exit instead of continuously protecting Moat 2.
  - **Repro / verification:** Command-backed check: `docker compose -f infra/deploy/docker-compose.yml --profile bitemporal-canary config` warned that `BITEMPORAL_CANARY_INTERVAL_SECONDS` is not set and defaulted it to a blank string.
  - **Suggested fix:** Escape the shell variable as `$${BITEMPORAL_CANARY_INTERVAL_SECONDS}` in both canary command blocks, or hardcode a default inside the container command.
  - **Section:** 8

- **[MEDIUM]** `.github/workflows/bitemporal-api-ci.yml:19` — The bitemporal CI trigger and static checks omit scoped support modules and all 0043-0051 migrations.
  - **Why it's a bug:** Changes to `research_observability.py`, `research_logging.py`, or migration SQL can skip this workflow or avoid the focused ruff/mypy checks. Those files contain auth, logging, PIT, and DDL behavior that the review spec treats as in scope. If not fixed, a PR can break the bitemporal API without running the bitemporal CI gate.
  - **Repro / verification:** `rg -n "research_observability|research_logging|2026050|paths:|ruff|mypy" .github/workflows/bitemporal-api-ci.yml` shows the omissions.
  - **Suggested fix:** Add all scoped API support modules, `src/aslan_core/db/migrations/versions/2026050*0[4-5][0-9]*.py`, `scripts/check_bitemporal_invariants.py`, and `scripts/canary_moat_2.py` to workflow triggers and static command inputs.
  - **Section:** 8

## LOW

### Section 5 / X3

- **[LOW]** `src/aslan_core/api/research_auth.py:88` — Public path checks use suffix matching.
  - **Why it's a bug:** `_is_public_path` returns true when a path merely ends with `/healthz`, `/version`, `/openapi.json`, or `/docs`. A future private path such as `/v1/research/admin/version` would bypass the master flag and auth dependencies. If not fixed, future route additions can accidentally become public.
  - **Repro / verification:** `rg -n "_PUBLIC_PATHS|endswith" src/aslan_core/api/research_auth.py`.
  - **Suggested fix:** Compare exact normalized paths against a full allowlist, or check route names instead of URL suffixes.
  - **Section:** 5 / X3

### Section 5 / X4

- **[LOW]** `src/aslan_core/api/research_observability.py:386` — Research audit middleware uses substring matching for the route boundary.
  - **Why it's a bug:** `if "/v1/research" not in request.url.path` treats any path containing that substring as a research route, including unrelated future paths such as `/internal/proxy/v1/research-status`. If not fixed, audit logs and redaction behavior can apply to non-research traffic.
  - **Repro / verification:** `rg -n "\"/v1/research\" not in request.url.path|startswith\\(\"/v1/research" src/aslan_core/api/research_observability.py`.
  - **Suggested fix:** Use `path == "/v1/research"` or `path.startswith("/v1/research/")` after normalizing trailing slashes and mount prefixes.
  - **Section:** 5 / X4

### Section 7

- **[LOW]** `docs/specs/bitemporal-research-api/README.md:26` — The README documents `/v1/verify/moat-2`, but the router exposes `/v1/research/verify/moat-2`.
  - **Why it's a bug:** Operators following the README will call a non-existent verification URL and may believe Moat 2 is unavailable. If not fixed, smoke-test instructions fail despite the route existing.
  - **Repro / verification:** `rg -n "/v1/verify/moat-2|verify/moat-2" docs/specs/bitemporal-research-api/README.md src/aslan_core/api/routes/research.py`.
  - **Suggested fix:** Update the README examples to `/v1/research/verify/moat-2`.
  - **Section:** 7

- **[LOW]** `docs/specs/bitemporal-research-api/RUNBOOK.md:24` — The deployment runbook says to apply migrations 0043-0050, omitting migration 0051.
  - **Why it's a bug:** Migration 0051 adds the `kap.disclosures` SCD-4 version table, trigger, and `kap.disclosures_at` function. If an operator follows the runbook exactly, disclosure PIT endpoints and invariant checks are missing the latest required schema. If not fixed, manual rollout can stop one migration short.
  - **Repro / verification:** `rg -n "0043-0050|0051|disclosures_at" docs/specs/bitemporal-research-api/RUNBOOK.md src/aslan_core/db/migrations/versions/20260509_0902_0051_kap_disclosures_scd4.py`.
  - **Suggested fix:** Change rollout language to 0043-0051, or to "upgrade to head" with explicit verification that 0051 is present.
  - **Section:** 7

- **[LOW]** `docs/specs/bitemporal-research-api/CHANGELOG.md:31` — The changelog still says the `ref.entity` bitemporal upgrade is deferred.
  - **Why it's a bug:** Migration 0046 implements `ref.entity_version`, triggers, and `ref.entity_at`, and HANDOFF marks the SCD-4 conversion done. If not fixed, reviewers and operators get contradictory status on whether entity PIT is expected to work.
  - **Repro / verification:** `rg -n "deferred|ref\\.entity|entity_version|entity_at" docs/specs/bitemporal-research-api/CHANGELOG.md docs/specs/bitemporal-research-api/HANDOFF.md src/aslan_core/db/migrations/versions/20260509_0705_0046_ref_entity_bitemporal_upgrade.py`.
  - **Suggested fix:** Update CHANGELOG to describe the implemented SCD-4 entity upgrade and reserve "deferred" only for genuinely open work.
  - **Section:** 7

### Section 11

- **[LOW]** `../docs/coordination/bitemporal-api-2026-05-09T05-54-35Z.lock:22` — The coordination lock file has stale commit status and still labels self-review as next.
  - **Why it's a bug:** The branch is at `c89de56`, but the lock file lists an older five-commit sequence and a pending self-review. If not fixed, coordination docs mislead reviewers about the actual branch state and remaining handoff work.
  - **Repro / verification:** `git rev-parse --short HEAD` returns `c89de56`; `rg -n "Commits|next|Self-review|c89de56" ..\\docs\\coordination\\bitemporal-api-2026-05-09T05-54-35Z.lock`.
  - **Suggested fix:** Refresh the lock file with current branch head, completed review status, and the current next action.
  - **Section:** 11

## Design Questions For Sidar

- Live Postgres checks were not available in this environment. Checks that require SSH to Hetzner Postgres or direct production/schema validation are deferred: check requires live Postgres; deferred.
- Full trigger execution tests that require Docker/testcontainers against PostgreSQL were not run here. Static migration checks and `pytest --collect-only` were run; trigger behavior still needs the live/containerized DB pass.
- Command-backed checks completed: OpenAPI validation passed; ruff passed for route and support modules; mypy passed for route and support modules under strict config; `tests/research` collection found 35 tests; boot smoke registered 15 `/v1/research` routes; D1-D30 coverage script found no missing decision IDs; `scripts/canary_moat_2.py` amendment constants passed the smoke check.
