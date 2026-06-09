# Bitemporal Research API — Comprehensive Bug Review (spec)

- **Status:** Spec for a review pass over the build at
  commit ≥ `70a6f82` on `aslan-core/feature/bitemporal-research-api`.
- **Audience:** A reviewer (human or agent) who reads this file
  cold and produces findings.
- **Drafted:** 2026-05-09. Update as new sections of the build land.

This spec breaks the build into **12 review sections** mapped to the
files actually written, plus **5 cross-cutting passes** that span
the whole surface. Each section enumerates what the section is
supposed to do, the bug-classes to hunt for, and concrete checks
with the commands/assertions that produce evidence.

The point is to find bugs that prior reviews missed — not to
revisit decisions. Re-litigating SCOPE.md decisions is out of scope;
verifying that the implementation actually satisfies them is in.

---

## 0. How to execute this spec

### Recommended path: parallel sections + serial cross-cutting

If a reviewer (e.g. a multi-agent dispatch) runs in parallel:

1. **Parallel — per-section reviews.** Sections 1–12 are
   file-disjoint enough that each can run independently. Dispatch
   one agent per section; each produces its own findings list.
2. **Serial — cross-cutting passes.** Sections X1–X5 read across
   multiple files and depend on the per-section findings being
   in. Run them after the parallel pass completes.
3. **Synthesis.** Aggregate findings into a single triage list,
   deduplicate, cross-reference, and prioritize by severity.

If a single reviewer (e.g. a human) runs in series, do
sections 1–12 in order, then X1–X5. Time-box each section.

### Severity scale

| Tier | Meaning | Example |
|---|---|---|
| **BLOCKER** | Cannot ship; will break in production at first request. | Schema mismatch (column doesn't exist); migration will fail to apply; auth bypass. |
| **HIGH** | Will break under realistic load or on a common edge case; correctness regression in Moat 2. | Audit-log writes silently fail and don't surface; PIT function returns wrong row at microsecond boundary. |
| **MEDIUM** | Contract drift; error envelope wrong; observability gap that masks operational signal. | Endpoint declares `pattern=` but accepts a value the OpenAPI rejects; missing CHANGELOG entry. |
| **LOW** | Code smell; minor doc gap; maintenance pain. | Magic number not extracted; file-level noqa where line-level would suffice. |
| **NIT** | Style; whitespace; cosmetic. Don't bother filing unless near other findings. |

Always pair severity with "what changes if we don't fix this." If
nothing changes, downgrade to NIT.

### Findings template

```markdown
- **[SEVERITY]** `path/to/file.py:LINE` — One-sentence description.
  - **Why it's a bug:** evidence — schema column / SCOPE.md
    reference / failed assertion.
  - **Repro / verification:** the command, query, or test that
    demonstrates the issue.
  - **Suggested fix:** the smallest change that resolves it,
    or "design call needed" if non-mechanical.
  - **Section:** N (per-section review I came from)
```

Aggregate findings into a single triage file at
`docs/specs/bitemporal-research-api/BUG_REVIEW_FINDINGS.md` after
all sections complete. Group by severity descending; within
severity, group by section.

### Acknowledged trade-offs (do NOT file)

These were deliberate choices documented elsewhere; flagging them
wastes review time:

- `kap.disclosures` and `ref.entity` use SCD-4 (current-state
  pointer + `_version` history table) rather than replacing the
  PK. Rationale: 14+ FKs depend on `ref.entity_pkey`; 4 FKs
  depend on `kap.disclosures_pkey`. See HANDOFF.md.
- `superseded_at` UPDATE pattern in `aslan-event-extractor` is
  intentionally broken by migration 0044's strict trigger. The
  cross-repo coordination is committed in the event-extractor
  repo (`70fb97b`).
- ADR-001/002/003 are PROVISIONAL stubs synthesized from existing
  CLAUDE.md content. Sidar's review is the path to FINAL, not a
  bug.
- `BITEMPORAL_API_ENABLED=false` master flag default is
  intentional per H7. Don't flag the inability to call endpoints
  in the default state.
- `KNOWN_AMENDMENTS` cases in the canary are real prod data; if
  the dollar amounts seem unusual, they're real published
  KAP-amendment values, not test fixtures.
- File-level `# ruff: noqa` was deliberately replaced in round 4
  with a typed helper. If a check still flags the helper, see
  Section 5.

---

## Per-section reviews

### Section 1 — Phase 1 design documents

**Files in scope**

```
docs/specs/bitemporal-research-api/SCOPE.md
docs/specs/bitemporal-research-api/OPENAPI.yaml
docs/specs/bitemporal-research-api/TESTPLAN.md
docs/specs/bitemporal-research-api/DECISIONS_LOG.json
docs/specs/bitemporal-research-api/PHASE_1_DECISION_DIFF.md
docs/specs/bitemporal-research-api/RESEARCH.md
docs/specs/bitemporal-research-api/EXISTING_PATTERNS_AUDIT.md
docs/specs/bitemporal-research-api/IMPLEMENTATION_NOTES.md
docs/decisions/ADR-001-batch-first-llm.md
docs/decisions/ADR-002-language-policy.md
docs/decisions/ADR-003-bloomberg-bar.md
```

**What this section is supposed to do**

Specify the binding contract: 30 decisions D1–D30 with
reversibility + feature-flag + status; 14 endpoints with full
OpenAPI 3.1 contract; ≥50 test cases tied to decisions; ADR-003
Moat 2 pinned; bitemporal best-practices research surveyed.

**Bug-classes to hunt**

1. **Decision drift.** SCOPE.md D-N says "X" but DECISIONS_LOG.json
   D-N says "Y." (e.g. reversibility tier mismatch, FF name
   different).
2. **Decision/code drift.** SCOPE.md says X is FINAL but the code
   doesn't satisfy X. (Cross-cutting; flag via this section then
   confirm via the relevant code section.)
3. **Decision missing FF.** Per H3 every MEDIUM/LOW reversibility
   decision must have a feature flag (D9 is the documented
   exception). Find any other exception.
4. **OpenAPI/route drift.** An endpoint in OPENAPI.yaml that
   doesn't exist in `routes/research.py`, or vice versa.
5. **OpenAPI schema/route shape drift.** OpenAPI says response is
   `Envelope<T>`; the route returns a bare list.
6. **TESTPLAN orphans.** A TC-NNN that no test references; or a
   test that doesn't tie to any TC-NNN.
7. **ADR-bound claim drift.** ADR-003 §"Moat 2 canary" says ≥10
   amendment cases; canary script has fewer.

**Concrete checks**

```bash
# 1. D1-D30 coverage
python -c "import json; d = json.load(open('docs/specs/bitemporal-research-api/DECISIONS_LOG.json')); ids = sorted(int(x['decision_id'][1:]) for x in d['decisions']); print('missing:', [i for i in range(1,31) if i not in ids])"

# 2. Reversibility-FF coverage (every MEDIUM/LOW must have a flag, except D9)
python -c "
import json
d = json.load(open('docs/specs/bitemporal-research-api/DECISIONS_LOG.json'))
print([x['decision_id'] for x in d['decisions']
       if x['reversibility'] in ('MEDIUM','LOW')
       and not x.get('feature_flag')
       and x['decision_id'] != 'D9'])
"
# Expected: []

# 3. OpenAPI vs router endpoints
grep -E '^  /' docs/specs/bitemporal-research-api/OPENAPI.yaml | sort > /tmp/openapi-paths.txt
grep -E '^@router\.(get|post)' src/aslan_core/api/routes/research.py | sed 's/.*"\(.*\)".*/\1/' | sort > /tmp/route-paths.txt
diff /tmp/openapi-paths.txt /tmp/route-paths.txt

# 4. OpenAPI 3.1 validity
uv run --with openapi-spec-validator openapi-spec-validator docs/specs/bitemporal-research-api/OPENAPI.yaml

# 5. TESTPLAN case count
grep -c "^### TC-" docs/specs/bitemporal-research-api/TESTPLAN.md  # expected >= 50

# 6. SCOPE.md FF inventory matches DECISIONS_LOG.json
grep -E "^\| \`BITEMPORAL_" docs/specs/bitemporal-research-api/SCOPE.md  # SCOPE.md §3 table
python -c "import json; d=json.load(open('docs/specs/bitemporal-research-api/DECISIONS_LOG.json')); ffs = sorted({x['feature_flag'] for x in d['decisions'] if x.get('feature_flag')}); print('\n'.join(ffs))"
# diff the two manually

# 7. ADR cross-reference reachability
grep -rn "ADR-001\|ADR-002\|ADR-003" docs/specs/bitemporal-research-api/ | head -20
ls docs/decisions/ 2>&1
# Expected: every ADR-NNN reference resolves to an existing file.
```

**Reviewer notes**

- `PHASE_1_DECISION_DIFF.md` lists D9 as the documented "FINAL with
  LOW reversibility, no FF" exception. Don't flag it.
- ADRs at workspace `docs/decisions/` are unversioned (workspace
  root has no `.git`). That's documented in HANDOFF; not a bug.
- RESEARCH.md is reference material, not normative. Spelling
  errors and citation gaps are NIT.

---

### Section 2 — Phase 2 schema migrations

**Files in scope**

```
src/aslan_core/db/migrations/versions/20260509_0700_0043_aslan_core_bitemporal_registry.py
src/aslan_core/db/migrations/versions/20260509_0701_0044_append_only_triggers_class_a.py
src/aslan_core/db/migrations/versions/20260509_0702_0045_canonical_financial_restatement_kind.py
src/aslan_core/db/migrations/versions/20260509_0703_0046_ref_entity_bitemporal_upgrade.py
src/aslan_core/db/migrations/versions/20260509_0704_0047_ref_entity_lineage.py
src/aslan_core/db/migrations/versions/20260509_0705_0048_research_api_support_tables.py
src/aslan_core/db/migrations/versions/20260509_0706_0049_pit_functions.py
src/aslan_core/db/migrations/versions/20260509_0707_0050_entity_quality_score_bitemporal.py
src/aslan_core/db/migrations/versions/20260509_0708_0051_kap_disclosures_bitemporal.py
docs/specs/bitemporal-research-api/PHASE_2_SHADOW_VALIDATION.sql
docs/specs/bitemporal-research-api/PHASE_2_DOWNGRADE.sql
```

**What this section is supposed to do**

Apply the bitemporal schema discipline to existing aslan-core
tables (Class A append-only triggers + registry + uniqueness),
add the API support tables (`api_key`, `api_query_audit`,
`api_rate_limit_state`, `feature_flags`), add SCD-4 history
tables for `ref.entity` and `kap.disclosures`, and create the
PIT SQL functions per D1.

**Bug-classes to hunt**

1. **Schema mismatch** — migration references a column that
   doesn't exist on the target table. The textbook example
   (already fixed in round 4): `e.name` / `e.kind` on
   `ref.entity` when the columns are `legal_name` / `entity_type`.
2. **PK / UNIQUE shape drift** — a PIT function's `DISTINCT ON`
   does not match the table's actual PK-minus-as_of. Documented
   instance: `ts.canonical_financial` PK is 11 columns; if the
   PIT function does `DISTINCT ON (entity_id, period_end)` only,
   it returns wrong rows.
3. **Asymmetric upgrade/downgrade** — `upgrade()` does X but
   `downgrade()` doesn't undo X (or vice versa). The
   reversibility cycle on shadow caught this in round 3 — but
   spot-check each migration's upgrade/downgrade pair manually.
4. **Trigger ordering hazards** — a new BEFORE/AFTER trigger fires
   in a position that breaks an existing trigger's contract.
   `kap.disclosures` already has `trg_mirror_to_doc_filing` and
   `trg_notify_body_ready`. Migration 0051 adds
   `trg_capture_disclosure_version`. Verify Postgres alphabetical
   ordering does not break `body_fetched`-fired logic.
5. **Idempotent SQL claims** — the SHADOW_VALIDATION.sql claims
   it's idempotent (uses `IF EXISTS` / `ON CONFLICT`); confirm by
   applying it twice on a fresh shadow.
6. **GRANT mismatch** — Class A bitemporal tables that should be
   readable by `aslan_dashboard` but aren't (or vice versa).
7. **Missing registry rows** — a Class A table with `as_of` in
   the schema but no row in `aslan_core.bitemporal_table_registry`.
   The H2 invariants check `bitemporal_table_registry_completeness`
   covers this; spot-check it actually finds violations on a
   shadow seeded with a test omission.
8. **Backfill correctness** — migrations 0046 and 0051 backfill
   `ref.entity_version` and `kap.disclosures_version` from the
   existing rows. Spot-check the COALESCE / WHERE logic produces
   1 row per existing row (no duplicates, no skips).

**Concrete checks**

```bash
# 1. Migration chain integrity
for f in src/aslan_core/db/migrations/versions/2026050*.py; do
    grep -E '^(revision|down_revision):' "$f" | tr '\n' ' '
    echo "($(basename $f))"
done
# Verify each down_revision points to the prior file's revision

# 2. Apply to a fresh shadow + run invariants
SHADOW="aslan_shadow_review_$(date +%s)"
ssh hetzner "echo 'CREATE DATABASE $SHADOW OWNER aslan;' | docker exec -i aslan-dashboard-postgres-1 psql -U postgres"
ssh hetzner "docker exec --user postgres aslan-dashboard-postgres-1 sh -c 'psql -U aslan -d $SHADOW -f /tmp/aslan_schema.sql >/dev/null && echo OK'"
cat docs/specs/bitemporal-research-api/PHASE_2_SHADOW_VALIDATION.sql | ssh hetzner "docker exec -i --user postgres aslan-dashboard-postgres-1 psql -U aslan -d $SHADOW -v ON_ERROR_STOP=1"
cat scripts/check_bitemporal_invariants.sql | ssh hetzner "docker exec -i --user postgres aslan-dashboard-postgres-1 psql -U aslan -d $SHADOW"
# Expected: 10 invariants, all 'ok'

# 3. Reversibility: apply forward, snap, downgrade, snap, apply forward, snap
# baseline -> up1 -> down -> up2; diff(up1, up2) excluding pg_dump nonce tokens MUST be empty
ssh hetzner "docker exec --user postgres aslan-dashboard-postgres-1 sh -c 'pg_dump -U aslan -s $SHADOW > /tmp/snap_up1.sql'"
cat docs/specs/bitemporal-research-api/PHASE_2_DOWNGRADE.sql | ssh hetzner "docker exec -i --user postgres aslan-dashboard-postgres-1 psql -U aslan -d $SHADOW -v ON_ERROR_STOP=1"
ssh hetzner "docker exec --user postgres aslan-dashboard-postgres-1 sh -c 'pg_dump -U aslan -s $SHADOW > /tmp/snap_down.sql'"
cat docs/specs/bitemporal-research-api/PHASE_2_SHADOW_VALIDATION.sql | ssh hetzner "docker exec -i --user postgres aslan-dashboard-postgres-1 psql -U aslan -d $SHADOW -v ON_ERROR_STOP=1"
ssh hetzner "docker exec --user postgres aslan-dashboard-postgres-1 sh -c 'pg_dump -U aslan -s $SHADOW > /tmp/snap_up2.sql ; diff /tmp/snap_up1.sql /tmp/snap_up2.sql | grep -v restrict | grep -v unrestrict'"
# Expected: empty diff.

# 4. Trigger fire test — attempt UPDATE on every Class A table
# (See tests/research/test_triggers.py for the canonical assertion.)
uv run pytest tests/research/test_triggers.py -m integration -v

# 5. Spot-check SCD-4 backfill row counts
cat <<'SQL' | ssh hetzner "docker exec -i --user postgres aslan-dashboard-postgres-1 psql -U aslan -d $SHADOW"
SELECT (SELECT count(*) FROM ref.entity) AS entities,
       (SELECT count(*) FROM ref.entity_version) AS entity_versions,
       (SELECT count(*) FROM kap.disclosures) AS disclosures,
       (SELECT count(*) FROM kap.disclosures_version) AS disclosure_versions;
SQL
# Expected: ref.entity_version >= ref.entity (one created + maybe one updated per row);
#           kap.disclosures_version >= kap.disclosures (one indexed + body_fetched if present).

# 6. Cleanup
ssh hetzner "echo 'DROP DATABASE $SHADOW;' | docker exec -i aslan-dashboard-postgres-1 psql -U postgres"
```

**Reviewer notes**

- Migrations 0045 and 0046 are intentional NO-OPs (TAS 29
  already covered by `restatement_basis`; ref.entity SCD-4
  replaces the original PK-change plan). Don't flag the empty
  bodies.
- The `# noqa: S608` on `_build_filtered_sql` is intentional —
  see Section 5.
- Backfill of `kap.disclosures_version` produces 2 rows for any
  disclosure with `body_fetched=true` (one `'indexed'`, one
  `'body_fetched'`). That's by design.

---

### Section 3 — Invariants script + canary

**Files in scope**

```
scripts/check_bitemporal_invariants.py
scripts/check_bitemporal_invariants.sql
scripts/canary_moat_2.py
```

**What this section is supposed to do**

H2 contract: a script that runs at every phase gate and returns
exit-0 iff every bitemporal invariant holds. H9 contract: a
canary that runs every 5 minutes against a curated list of
amendments and exits 0 iff every case still returns the right
PIT value.

**Bug-classes to hunt**

1. **False-negative invariant** — invariant SQL returns 0 when
   data is actually broken. Inject a deliberate violation on a
   shadow (e.g. `INSERT ... ON CONFLICT DO UPDATE` to bypass the
   trigger, or temporarily drop the trigger) and confirm the
   invariant flags it.
2. **False-positive invariant** — invariant SQL returns >0 when
   data is fine. Common cause: forgetting to filter inheritance
   children (TimescaleDB chunks) — already fixed once, may
   regress if a new partition shape ships.
3. **Canary case drift** — `KNOWN_AMENDMENTS` references
   `entity_id` / `canonical_code` / `as_of` values that no longer
   exist in production (e.g. data archived). Run the canary
   against staging and confirm 10/10 green.
4. **Canary persistence side-effect** — the canary writes
   `MOAT_2_CANARY_STATUS` to `aslan_core.feature_flags`. Confirm
   the persist function uses `ON CONFLICT DO UPDATE` (not a bare
   INSERT that double-writes).
5. **Script log/output coupling** — the canary emits BOTH JSON to
   stdout (cron contract) AND structured log events. Confirm
   cron-script consumers parse cleanly when both are emitted
   (stderr vs stdout separation must be respected).
6. **DSN handling** — both scripts redact passwords via a
   `_redact_dsn` helper. Confirm the redaction handles every
   form: `postgresql://user:pass@host/db`,
   `postgresql+asyncpg://...`, percent-encoded passwords.

**Concrete checks**

```bash
# 1. Invariant SQL parse-check (Postgres planner only; no execution)
cat scripts/check_bitemporal_invariants.sql | ssh hetzner "docker exec -i --user postgres aslan-dashboard-postgres-1 psql -U aslan -d aslan -c 'BEGIN; PREPARE p AS \$1; ROLLBACK;' --set ON_ERROR_STOP=1" 2>&1 | head -5
# (Approximation — for true parse-check feed each invariant SQL through EXPLAIN)

# 2. Invariant false-negative spot-check
# On a fresh shadow, deliberately break ts.observation by dropping the no_update trigger
# and inserting a duplicate (series_id, ts, as_of); the invariant must catch it.

# 3. Canary cases against prod schema
python -c "
from scripts.canary_moat_2 import KNOWN_AMENDMENTS
assert len(KNOWN_AMENDMENTS) >= 10
for c in KNOWN_AMENDMENTS:
    assert c.expected_before is None or isinstance(c.expected_before, float)
    assert c.expected_before != c.expected_after  # amendment must change value
print('OK', len(KNOWN_AMENDMENTS), 'cases')
"

# 4. _redact_dsn coverage
python -c "
from scripts.canary_moat_2 import _redact_dsn
import re
cases = [
    'postgresql://aslan:secret@host:5432/aslan',
    'postgresql+asyncpg://user:p%40ss@host/db',
    'postgresql://aslan@host/db',  # no password
]
for c in cases: print(_redact_dsn(c))
# Expected: secret/p%40ss replaced; no-password unchanged
"
```

**Reviewer notes**

- Both scripts intentionally emit JSON on stdout for cron
  consumers and structured events on stderr (via structlog).
  Don't flag this as inconsistency.
- `KNOWN_AMENDMENTS` cases are real prod data; values may look
  unusual.

---

### Section 4 — API router (`routes/research.py`)

**Files in scope**

```
src/aslan_core/api/routes/research.py
```

**What this section is supposed to do**

Implement the 14 endpoints per OPENAPI.yaml. Each endpoint:
parses `as_of` (rejecting naive datetimes per D13), enforces auth
+ rate limit + master flag, calls the corresponding PIT SQL
function or table, builds an envelope per D15, applies cache
headers per D17, supports cursor pagination per D7, and emits the
appropriate audit-log fields via `RequestContext`.

**Bug-classes to hunt**

1. **Schema column mismatch** — the SQL `SELECT col_x FROM
   schema.table` references `col_x` that doesn't exist (the
   `e.name` / `e.kind` bug was already a HIGH severity find).
   Spot-check every endpoint against the actual table schema.
2. **PIT-function call signature** — endpoint calls
   `ts.observation_at(:as_of)` but the function signature is
   `(p_as_of timestamptz)` with no default; ensure the parameter
   binding type matches.
3. **`Pagination(has_more=...)`** drift — endpoints that compute
   `has_more` differently from `(next_cursor is not None)`. Should
   be uniform now.
4. **`ctx` field omission** — an endpoint forgets to set
   `ctx.api_key_id` / `ctx.rate_tier` / `ctx.rows_returned` / one
   of the other fields. The audit row will be missing data.
5. **Cache header drift** — `set_cache_headers(response, ...)`
   not called, OR called with the wrong `as_of_resolved`.
6. **Cursor `filters_for_cursor` drift** — the dict passed to
   `encode_cursor` / `decode_cursor` doesn't include all the
   active filter values. Cursor reuse with a different filter
   would silently succeed.
7. **PII redaction gap** — `/events` redacts `payload`, but other
   endpoints could also surface counterparty names (e.g. if
   `agg.entity_resolution_queue` is exposed in v2). For v1, only
   `/events` matters; confirm the helper covers every key in
   `_PII_KEYS`.
8. **Error-shape drift** — RFC 7807 says the body has
   `type/title/status/detail/instance/code`. Confirm every
   `HTTPException(detail=...)` is dict-shaped, not bare string,
   so the RFC 7807 handler can extract `code` / `title`.
9. **Async leak** — `await session.execute(...)` followed by
   `.first()` (which is sync). With `AsyncSession` results,
   `result.first()` is fine but `result.all()` returns a list; some
   endpoints use one, some use the other.
10. **`Depends(...)` order matters** — `enforce_rate_limit` reads
    `ctx` (via `get_principal` → `get_session`); confirm no
    endpoint declares `enforce_rate_limit` BEFORE `get_session`
    in a way that breaks dependency resolution.
11. **`ctx.feature_flags_active`** is set from `_active_flags`
    *before* the response is built. If a flag was flipped
    mid-request, the snapshot may not reflect it. Confirm this is
    intentional (it is — "snapshot at request start" is the
    documented contract).

**Concrete checks**

```bash
# 1. Endpoint count vs OpenAPI
grep -c '^@router\.(get|post)' src/aslan_core/api/routes/research.py
grep -c '^  /' docs/specs/bitemporal-research-api/OPENAPI.yaml

# 2. Every endpoint depends on enforce_rate_limit (or is public)
# Public: /healthz, /version, /verify/moat-2, /openapi.json
grep -B5 "@router.get" src/aslan_core/api/routes/research.py | grep -E "enforce_rate_limit|@router.get" | head -50

# 3. Every list endpoint sets a next_cursor or has_more
grep -E "(Pagination|next_cursor|encode_cursor)" src/aslan_core/api/routes/research.py | head -20

# 4. Schema column references — verify against actual schema
# Compare every SELECT in research.py with the corresponding \d output
ssh hetzner "docker exec -i --user postgres aslan-dashboard-postgres-1 psql -U aslan -d aslan -c '\d ts.observation'" | head -10
# (Repeat for every table referenced)

# 5. Lint + type
uv run ruff check src/aslan_core/api/routes/research.py
uv run mypy --strict src/aslan_core/api/routes/research.py

# 6. Boot smoke test
uv run python -c "
from aslan_core.api import create_api_app
app = create_api_app()
paths = [r.path for r in app.routes if hasattr(r, 'path')]
assert '/v1/research/healthz' in paths
assert '/v1/research/openapi.json' in paths
print('routes:', sorted(p for p in paths if '/v1/research' in p))
"
```

**Reviewer notes**

- The `# ruff: noqa: S608` was deliberately removed; the helper
  `_build_filtered_sql` localizes the silence. If ruff flags
  S608 anywhere outside that helper, that's a real find.
- `_CLOCK_SKEW_TOLERANCE = timedelta(seconds=60)` is at module
  level; if `_parse_as_of` references it before definition,
  Python catches it at import; flag if so.
- Cursor pagination's `decode_cursor` is wired (validates
  filters_hash) but the keyset-WHERE application is a TODO. If a
  `cursor=...` is supplied, the page returns from the TOP, not
  resumed. That's documented at every site with
  `# TODO(D7): apply keyset WHERE ...`. Don't file this as a
  bug; flag only if the TODO comments are missing.

---

### Section 5 — Supporting modules (envelope, auth, observability, logging)

**Files in scope**

```
src/aslan_core/api/research_envelope.py
src/aslan_core/api/research_auth.py
src/aslan_core/api/research_observability.py
src/aslan_core/api/research_logging.py
```

**What this section is supposed to do**

Cross-cutting helpers consumed by every route handler. The
envelope shape is D15; auth is D5 with argon2id; observability
covers D6/D7/D17/D18/D25; logging is D16+D25.

**Bug-classes to hunt**

1. **`build_envelope` field omission** — the dict returned
   doesn't include every D15 metadata field (lineage,
   pagination, feature_flags_active, warnings, request_id,
   served_by).
2. **Argon2id verify mismatch** — `verify_secret` accepts a
   non-`$argon2`-prefixed hash via the `hmac.compare_digest`
   fallback. That's intentional for seed/test keys, but a
   misconfigured production key (e.g., DB seeded with the wrong
   hash format) silently auth-bypasses if the stored value
   matches the presented secret literally. Confirm this is
   gated by an environment-specific check or removed in v1.
2a. **Constant-time compare leak** — argon2 verify is
    constant-time; the legacy `hmac.compare_digest` path is also
    constant-time. Confirm no `==` compare on secret material
    elsewhere.
3. **`_PUBLIC_PATHS` drift** — the `_is_public` helper checks
   path-suffix endsWith. If `/v1/research/version-2` is added
   later, `_is_public` returns true for it because it ends with
   `/version`. Convert to `path.split('/')[-1] in PUBLIC_PATHS`
   for safety.
4. **Rate-limit token-bucket race** — UPSERT `tokens_used =
   tokens_used + 1` is the atomic increment. Confirm by
   inspection. False-positive: `INSERT ... DO UPDATE` in
   Postgres is atomic.
5. **Audit-log middleware path filter** —
   `if "/v1/research" not in request.url.path` is a substring
   check; `/api/something/v1/research-not` would falsely match.
   Use `startswith` instead.
6. **RFC 7807 handler order** — `register_research_exception_handlers`
   must run before `register_exception_handlers`; confirm the
   call order in `api/__init__.py` and that FastAPI dispatches
   in registration order.
7. **`bind_request_log_context` cleanup** — the contextmanager
   uses a `try/finally`; confirm `clear_contextvars()` runs even
   if the route raises.
8. **`update_request_log_context` outside binding** — calling it
   without an active `bind_request_log_context` is a no-op (per
   the contextvar guard). Confirm it doesn't raise; otherwise
   tests/scripts importing it without setting up context will
   fail.
9. **Prometheus `_NoOp` typing** — when `prometheus_client` is
   missing, the module-level vars are `_NoOp` instances typed as
   `Any`. Confirm the `Any` typing doesn't bleed into call sites
   (a typo `_REQ_TOTAL.lable(...)` would only fail at runtime).
10. **`_query_params_sha256` PII leak risk** — hash of canonical
    query params. If query params include sensitive values (e.g.
    a session token in the URL), they hit the audit table as
    bytes hash — uninvertible but the original was already on
    the wire. Confirm we're not logging raw request URLs anywhere
    that this hash compromises.

**Concrete checks**

```bash
# 1. Envelope completeness
uv run python -c "
from aslan_core.api.research_envelope import build_envelope, EnvelopeMetadata
import uuid, datetime, json
env = build_envelope(
    data=[],
    request_id=uuid.uuid4(),
    as_of_requested=None,
    as_of_resolved=datetime.datetime.now(datetime.timezone.utc),
)
assert 'data' in env
assert 'metadata' in env
m = env['metadata']
expected = {'as_of_requested', 'as_of_resolved', 'as_of_range', 'lineage',
            'pagination', 'feature_flags_active', 'warnings', 'request_id',
            'served_by'}
missing = expected - set(m.keys())
print('missing:', missing)  # expected: empty
"

# 2. Argon2id round-trip
uv run python -c "
from aslan_core.api.research_auth import hash_secret, verify_secret
h = hash_secret('topsecret')
assert h.startswith('\$argon2')
assert verify_secret(h, 'topsecret') is True
assert verify_secret(h, 'wrong') is False
# Legacy fallback
assert verify_secret('not-an-argon-hash', 'not-an-argon-hash') is True
assert verify_secret('not-an-argon-hash', 'wrong') is False
print('OK')
"

# 3. Logging context bind/unbind
uv run python -c "
from aslan_core.api.research_logging import (
    configure_research_logging, bind_request_log_context, get_research_logger
)
import structlog, uuid
configure_research_logging()
log = get_research_logger('test')
with bind_request_log_context(request_id=uuid.uuid4(), endpoint='/x', api_key_id=None):
    log.info('inside')
log.info('outside')  # must NOT carry request_id
print('OK')
"
# Manually inspect output: 'outside' must not have request_id field.

# 4. Lint + type
uv run ruff check src/aslan_core/api/research_envelope.py src/aslan_core/api/research_auth.py src/aslan_core/api/research_observability.py src/aslan_core/api/research_logging.py
uv run mypy --strict src/aslan_core/api/research_envelope.py src/aslan_core/api/research_auth.py src/aslan_core/api/research_observability.py src/aslan_core/api/research_logging.py
```

**Reviewer notes**

- The `_redact_dsn` helper deliberately uses regex; consider
  edge cases for percent-encoded passwords.

---

### Section 6 — Phase 4 tests

**Files in scope**

```
tests/research/__init__.py
tests/research/test_invariants.py
tests/research/test_pit_functions.py
tests/research/test_triggers.py
tests/research/test_endpoints.py
tests/research/test_endpoints_extended.py
```

**What this section is supposed to do**

Cover at minimum the high-leverage TC-NNN cases from TESTPLAN.md.
Tests run via `pytest -m integration` against a testcontainers
Postgres. The default `pyproject.toml addopts` deselects
integration tests so unit-test runs don't try to spin Postgres.

**Bug-classes to hunt**

1. **Test marker missing** — a test in `tests/research/` not
   marked `pytest.mark.integration`. It would run by default
   without a Postgres available and fail hard.
2. **TC-NNN drift** — test docstring cites TC-027 but TESTPLAN
   says TC-027 is about something else entirely.
3. **Fixture leakage** — a test seeds `agg.filing_event` with
   row R; subsequent tests see R because the fixture isn't
   transactional / isn't rolled back.
4. **Schema-mismatch hidden by 401/503** — a test happens to
   pass because the master flag is off (503) or auth is missing
   (401), but if it actually reached the SQL it would fail. The
   test should assert the SQL path was reached when relevant.
5. **Direct-helper test bypasses HTTP path** — e.g.
   `test_naive_as_of_rejected` calls `_parse_as_of` directly
   rather than going through the route. Acceptable for unit
   contract but doesn't verify the route wires the parser.
   Confirm at least one HTTP-level test exists per parser
   contract.
6. **Mock leakage** — a test mocks the engine but a fixture
   below it expects a real engine.
7. **Async-loop conflict** — Starlette `TestClient` creates its
   own event loop; mixing with the session-scoped async engine
   causes "loop is closed" errors. The existing tests use sync
   psycopg for flag manipulation precisely to avoid this; flag
   any new async-engine usage in tests.

**Concrete checks**

```bash
# 1. Every test file marks integration
grep -E "pytestmark = pytest.mark.integration|@pytest.mark.integration" tests/research/*.py | head -10
# Expected: every file has the module-level mark or every test has the decorator.

# 2. Test count
uv run pytest tests/research/ --collect-only -m integration -q 2>&1 | tail -3
# Expected: >= 35 tests.

# 3. Run the trigger tests against a real Postgres (requires Docker)
uv run pytest tests/research/test_triggers.py -m integration -v

# 4. TC-NNN coverage
grep -oE "TC-[0-9]+" tests/research/*.py | sort -u | wc -l  # tests cover N TC numbers
grep -c "^### TC-" docs/specs/bitemporal-research-api/TESTPLAN.md  # total TCs in plan
# Coverage ratio is informational, not a gate.
```

**Reviewer notes**

- Some tests intentionally exercise helpers (e.g. `_parse_as_of`)
  rather than HTTP. That's acceptable; the helper IS the contract.
- `tests/research/test_endpoints_extended.py` notes
  intentionally narrow scope per its agent dispatch.

---

### Section 7 — Phase 5 docs (README, RUNBOOK, CHANGELOG)

**Files in scope**

```
docs/specs/bitemporal-research-api/README.md
docs/specs/bitemporal-research-api/RUNBOOK.md
docs/specs/bitemporal-research-api/CHANGELOG.md
docs/specs/bitemporal-research-api/HANDOFF.md
docs/specs/bitemporal-research-api/AUDIT_TRAIL.md
```

**What this section is supposed to do**

Customer-facing docs (README) and operator-facing docs (RUNBOOK).
CHANGELOG follows Keep-a-Changelog. HANDOFF reflects current state.
AUDIT_TRAIL is append-only chronology.

**Bug-classes to hunt**

1. **Doc drift** — README example uses an endpoint or response
   field that doesn't match the current code.
2. **RUNBOOK incident-procedure validity** — a RUNBOOK procedure
   references a SQL function or table that doesn't exist
   (e.g. references `aslan_core.api_query_audit` before
   migration 0048 lands).
3. **CHANGELOG date drift** — entries dated in the future or
   referencing commits that don't exist.
4. **HANDOFF stale claims** — the "deferred" section lists
   items that have since been implemented (already caught in
   round 4 self-review; verify the latest update).
5. **CURL example correctness** — the `X-Aslan-Api-Key:
   key_id:secret` header format must match what
   `research_auth.py` parses. README shows the format; verify.
6. **Turkish content fidelity** — README example uses Turkish
   for KAP disclosure title; confirm the diacritics survive
   markdown rendering and aren't ASCIIfied.

**Concrete checks**

```bash
# 1. README curl examples actually parse the right header
grep -E "X-Aslan-Api-Key" docs/specs/bitemporal-research-api/README.md
grep -E "X-Aslan-Api-Key" src/aslan_core/api/research_auth.py
# The format must match (key_id:secret per research_auth.py)

# 2. RUNBOOK SQL queries reference real tables/columns
grep -E "aslan_core\.|/v1/research/" docs/specs/bitemporal-research-api/RUNBOOK.md | head -20
# Cross-check against migrations/research.py

# 3. CHANGELOG dates / versions
grep -E "^## \[" docs/specs/bitemporal-research-api/CHANGELOG.md | head -10
# Versions strictly increasing; dates not in the future

# 4. HANDOFF "deferred" section coherence
grep -B2 -A5 "deferred\|DONE\|implemented" docs/specs/bitemporal-research-api/HANDOFF.md | head -30
# No item in "deferred" should also be in "DONE"
```

**Reviewer notes**

- `AUDIT_TRAIL.md` is append-only by contract; entries are
  ordered oldest-first. Don't flag duplicate "Round N close"
  headings in different rounds.

---

### Section 8 — Phase 5 deploy + CI

**Files in scope**

```
.github/workflows/bitemporal-api-ci.yml
infra/deploy/docker-compose.yml  (only the bitemporal-canary entry)
```

**What this section is supposed to do**

CI runs on push: validates OpenAPI, runs ruff + mypy on the new
files. Compose has a `bitemporal-canary` profile-gated service
that runs `scripts/canary_moat_2.py` every 5 minutes.

**Bug-classes to hunt**

1. **CI scoping miss** — workflow only runs on
   `feature/bitemporal-*` push; if the branch is renamed or
   merged to `main`, CI doesn't run. Confirm `pull_request` on
   `main` is also a trigger.
2. **CI version drift** — workflow uses `actions/setup-uv@v7`
   but the project's `uv` version pin in `pyproject.toml` is
   different. Mismatch on `uv` resolves to a different lockfile.
3. **CI typo** — `uv run mypy --strict` paths that don't exist
   silently pass.
4. **Compose service syntax** — `profiles: [bitemporal-canary]`
   is the right shape; verify with `docker compose config`.
5. **Compose dependency** — canary needs `aslan-dashboard-postgres-1`
   to be up. If `depends_on` is missing, the canary container
   crashes on first cycle.
6. **`BITEMPORAL_CANARY_INTERVAL_SECONDS` env var** — referenced
   in the loop; confirm it's defined in the compose env and has
   a sensible default.

**Concrete checks**

```bash
# 1. CI workflow validates
yq eval '.jobs | keys' .github/workflows/bitemporal-api-ci.yml

# 2. CI commands resolve
yq eval '.jobs | to_entries[] | .value.steps[] | select(.run) | .run' .github/workflows/bitemporal-api-ci.yml

# 3. Compose config
docker compose -f infra/deploy/docker-compose.yml --profile bitemporal-canary config 2>&1 | head -50
```

**Reviewer notes**

- The CI intentionally skips DB-backed tests (no Postgres in CI);
  documented in a comment in the workflow file.

---

### Section 9 — Cross-repo: aslan-event-extractor SCOPE_v2 D6

**Files in scope (separate repo)**

```
aslan-event-extractor/SCOPE_v2.md  (the patched D6 section)
aslan-event-extractor/CHANGELOG.md
```

**What this section is supposed to do**

Document the new-row supersession pattern that aligns with
aslan-core migration 0044's strict trigger.

**Bug-classes to hunt**

1. **Wording drift** — D6 still describes UPDATE-based
   supersession somewhere (e.g. in code samples below the
   decision body).
2. **Cross-reference rot** — D6 cites aslan-core migrations 0044
   / 0049; verify those revision IDs match the actual files in
   aslan-core.
3. **Historical callout drift** — the "Historical (pre-2026-05-09)"
   block should preserve the v1 D6 wording verbatim. Spot-check
   against the `SCOPE.md` (v1) source.

**Concrete checks**

```bash
# 1. D6 in SCOPE_v2 references the right migration revisions
grep -E "0044|0049" aslan-event-extractor/SCOPE_v2.md

# 2. No remaining "UPDATE OF superseded_at" prescription in D6 active body
grep -B2 -A20 "^### D6" aslan-event-extractor/SCOPE_v2.md | grep -i "UPDATE.*superseded"
# Expected: only inside the Historical callout, if anywhere.
```

**Reviewer notes**

- This is a separate git repo. Findings go to the
  aslan-event-extractor maintainer (sidar), not the aslan-core PR.

---

### Section 10 — ADRs

**Files in scope**

```
docs/decisions/ADR-001-batch-first-llm.md
docs/decisions/ADR-002-language-policy.md
docs/decisions/ADR-003-bloomberg-bar.md
```

**What this section is supposed to do**

Codify the strategic decisions: batch-first LLM, Turkish-verbatim
+ Python-first, Bloomberg bar with seven moats.

**Bug-classes to hunt**

1. **Internal contradiction** — ADR-002 says "Python-first SDK"
   but later mentions a TypeScript SDK as planned. (Minor; nit.)
2. **Cross-reference rot** — ADR-003 §"Moat 2 canary" lists ≥10
   amendments; canary script has fewer. (Already verified to be
   exactly 10; confirm.)
3. **Provenance honesty** — ADRs are PROVISIONAL stubs synthesized
   from CLAUDE.md content. Confirm the "Status: PROVISIONAL"
   line is present at the top of each.

**Concrete checks**

```bash
grep -E "^- \*\*Status:" docs/decisions/ADR-00*.md
# Expected: every ADR has a Status line.
```

**Reviewer notes**

- ADRs are unversioned (workspace `docs/decisions/` is not in any
  git repo). Don't flag the absence of a commit.

---

### Section 11 — Coordination + lock file

**Files in scope**

```
docs/coordination/bitemporal-api-2026-05-09T05-54-35Z.lock
```

**What this section is supposed to do**

H4 protocol claim file. After the build is complete, status
should be `complete` with the commit log up to date.

**Bug-classes to hunt**

1. **Stale heartbeat** — `last_heartbeat` is hours/days old but
   `status: active`. Should be `complete` or `partial`.
2. **Commit log drift** — list of commits in the lock file
   doesn't match `git log` on the feature branch.
3. **Shadow DB list outdated** — lock file lists shadow DBs that
   no longer exist on Hetzner (already cleaned up) or omits ones
   that do.

**Concrete checks**

```bash
# 1. Compare commits in lock file with git log
grep "^  - \`" docs/coordination/bitemporal-api-*.lock | head -20
git log --oneline feature/bitemporal-research-api ^09f5dcf | head -20

# 2. Shadow DB enumeration
ssh hetzner "docker exec aslan-dashboard-postgres-1 psql -U postgres -tAc \"SELECT datname FROM pg_database WHERE datname LIKE 'aslan_shadow%'\""
```

**Reviewer notes**

- Shadow DBs left from prior rounds are intentional (sidar
  cleans up after review). Their presence is not a bug.

---

### Section 12 — `pyproject.toml` + dependencies

**Files in scope**

```
pyproject.toml
uv.lock
```

**What this section is supposed to do**

Declare the runtime + test deps for the new code (psycopg with
binary, argon2-cffi). Existing extras
(`[obs]`, `[dashboard]`, `[api]`) unchanged.

**Bug-classes to hunt**

1. **Dependency-pinning regression** — adding new deps caused
   transitive resolution to bump an unrelated package, breaking
   `mypy --strict` elsewhere.
2. **Extras leak** — a dep that should be in `[obs]` ended up
   in the base `dependencies` (e.g. `prometheus-client`).
3. **Lockfile staleness** — `uv.lock` not regenerated after a
   `uv add`; `uv sync` would diverge.

**Concrete checks**

```bash
diff <(uv tree --no-dedupe | head -40) <(cat /tmp/uv-tree-snapshot.txt)
uv lock --check  # exits non-zero if lockfile is stale
```

**Reviewer notes**

- `psycopg[binary]` is intentional in base deps (the canary
  script imports `psycopg` directly).

---

## Cross-cutting passes

### X1 — SCOPE.md ↔ implementation drift sweep

**What:** For every binding decision D1–D30, verify the
implementation actually does what SCOPE.md says.

**How:**
1. Read SCOPE.md decision N.
2. Find the corresponding code site (grep by the decision
   ID's keyword: e.g. D7 → cursor; D17 → cache; D18 → audit_log).
3. Confirm the code does what the decision body claims.
4. If the decision is FF-gated, confirm the FF defaults match
   the SCOPE table.

**Most-likely-to-drift decisions**: D2 (interval), D6 (rate
limit thresholds), D7 (cursor TODO is documented), D8 (TAS 29
chain — schema already had it), D17 (cache 5-minute window),
D19 (cost limits — implemented? grep for `QUERY_TOO_LARGE`).

### X2 — OpenAPI ↔ route shape sweep

**What:** For every endpoint, the response shape declared in
OPENAPI.yaml must match the dict the route returns.

**How:** For each endpoint, dispatch a request via TestClient
(with seeded data) and compare the response keys against the
OpenAPI schema's `properties`.

### X3 — Logging coverage sweep

**What:** Every error path that raises a 4xx or 5xx must emit a
structured log event before the response is built.

**How:** grep for `raise HTTPException` across the API surface;
for each occurrence, confirm an adjacent `logger.warning` /
`logger.error` / `logger.exception` exists or that the RFC 7807
handler will emit one.

```bash
grep -nE "raise HTTPException" src/aslan_core/api/routes/research.py src/aslan_core/api/research_auth.py src/aslan_core/api/research_observability.py
# For each, verify a log event covers it (either inline or via
# the global RFC 7807 handler, which DOES log at WARNING/ERROR).
```

### X4 — Audit-log completeness sweep

**What:** Every authenticated request must produce exactly one
row in `aslan_core.api_query_audit`. Public requests
(`/healthz`, `/version`, `/verify/moat-2`, `/openapi.json`)
should produce a row with `api_key_id IS NULL`.

**How:** Replay each endpoint via TestClient; query the audit
table after each request; confirm row count.

### X5 — Schema-vs-code consistency

**What:** Every column referenced in `routes/research.py` SQL
must exist in the actual prod schema. Catches the
`e.name` / `e.kind` class of bug.

**How:** for each `SELECT col1, col2 FROM schema.table`,
introspect the schema:

```bash
ssh hetzner "docker exec -i --user postgres aslan-dashboard-postgres-1 psql -U aslan -d aslan -c '\\d schema.table'"
```

Diff the column names in the SELECT against the table's actual
columns. Mismatch = BLOCKER.

---

## Output artifact

The reviewer writes findings to:

```
docs/specs/bitemporal-research-api/BUG_REVIEW_FINDINGS.md
```

with:
1. Top: severity counts + run-summary (`N findings; X BLOCKER, Y HIGH, Z MEDIUM, W LOW, V NIT`).
2. Body: findings grouped by severity descending, then by
   section, then by file path.
3. Each finding follows the template in §0.
4. Bottom: a short "design questions for sidar" section for
   things that the reviewer thinks are bugs but might be
   intentional trade-offs not yet documented.

After writing the findings file, optionally raise an issue per
BLOCKER/HIGH on the GitHub PR (#25) so the engineer driving
the build sees them inline with the diff.
