# Bitemporal Research API — RUNBOOK

Operational runbook for the Aslan Terminal Bitemporal Research API.
Pairs with [SCOPE.md](./SCOPE.md) (binding decisions) and
[HANDOFF.md](./HANDOFF.md) (current state). See the platform-level
[`HETZNER_OPS.md`](../../../../HETZNER_OPS.md) for SSH plumbing,
container inventory, and standard ops patterns.

---

## 1. Deployment

### Staging

```powershell
# 1. Push branch to GitHub.
cd "C:\Users\Sidar\Desktop\aslan 2\aslan-core"
git push origin feature/bitemporal-research-api

# 2. Pull on Hetzner (staging compose runs against the same DB; the
#    research router is gated by BITEMPORAL_API_ENABLED).
ssh hetzner 'cd ~/aslan-core && git fetch && git checkout feature/bitemporal-research-api && git pull --ff-only'

# 3. Apply migrations to staging (alembic; 0043-0050).
ssh hetzner 'cd ~/aslan-core/infra/deploy && docker compose run --rm --entrypoint "alembic upgrade head" api'

# 4. Verify invariants.
ssh hetzner 'cd ~/aslan-core && docker compose run --rm --entrypoint "python scripts/check_bitemporal_invariants.py" api'

# 5. Rebuild and restart the API container.
ssh hetzner 'cd ~/aslan-core/infra/deploy && docker compose build api && docker compose up -d api'
```

### Production (shadow-first protocol per Phase 7b)

Production deploys are gated by the `PROMOTE_TO_PROD` workspace marker
(out-of-band sidar action). The protocol:

1. Clone production DB into a Hetzner shadow:
   `pg_dump aslan | docker exec -i aslan-dashboard-postgres-1 psql -U aslan -d aslan_shadow_<ts>`.
2. Apply 0043-0050 to the shadow; run
   `scripts/check_bitemporal_invariants.py` against shadow DSN — must
   be 10/10.
3. Run `scripts/canary_moat_2.py --target shadow --no-persist` — must
   be green.
4. Apply migrations to production *with master flag off*:
   `BITEMPORAL_API_ENABLED=false`. Endpoints return 503 until flipped.
5. Monitor invariants for 1 hour; if stable, flip the master flag (§3).
6. Run canary live for 1 hour; if stable, leave on. If red at any
   point, follow §6 Canary-red triage.

---

## 2. Master flag flip

The master flag `BITEMPORAL_API_ENABLED` lives in
`aslan_core.feature_flags.value_bool` and overrides every other flag.
When false, every research endpoint returns
`503 FEATURE_DISABLED` (verify with §10 Kill-switch).

```sql
-- Flip ON (production-safe; takes effect on next request, no restart).
UPDATE aslan_core.feature_flags
   SET value_bool = true,
       updated_at = NOW(),
       notes      = COALESCE(notes,'') ||
                    E'\nflipped ON by sidar at ' || NOW()::text
 WHERE flag_name = 'BITEMPORAL_API_ENABLED';

-- Verify.
SELECT flag_name, value_bool, updated_at
  FROM aslan_core.feature_flags
 WHERE flag_name = 'BITEMPORAL_API_ENABLED';
```

Run via:

```powershell
ssh hetzner 'docker exec -i aslan-dashboard-postgres-1 psql -U aslan -d aslan' < flip_master.sql
```

---

## 3. API key management

### Issue a new key

```sql
-- 1. Hash the secret with argon2id BEFORE inserting (one-liner below).
-- 2. Insert.
INSERT INTO aslan_core.api_key
       (key_id, secret_hash, rate_tier, pii_unredacted,
        description, expires_at)
VALUES (gen_random_uuid(),
        '<argon2id-hash>',
        'partner',
        false,
        'Issued for Acme Research; partner tier; 1y',
        NOW() + INTERVAL '1 year')
RETURNING key_id;
```

### Hash a secret with argon2id (one-liner)

```powershell
# Generate a 32-byte secret and its argon2id hash. Save BOTH outputs:
# - line 1 = the plaintext secret to give the customer (only shown here)
# - line 2 = the secret_hash to insert
uv run python -c "import secrets; from argon2 import PasswordHasher; s=secrets.token_urlsafe(32); print(s); print(PasswordHasher().hash(s))"
```

### Revoke a key

```sql
UPDATE aslan_core.api_key
   SET revoked_at = NOW()
 WHERE key_id = '<uuid>';
```

The auth dependency in `research_auth.py::get_principal` checks
`revoked_at IS NULL` on every request; revocation is effective on the
next request.

---

## 4. Rate-limit tuning

Per-key overrides live in `aslan_core.api_key.rate_overrides` (jsonb).
Schema: `{"per_minute": int, "per_hour": int, "per_day": int}` — any
subset of keys overrides the tier defaults from D6.

```sql
-- Bump Acme Research to 1200 rpm without changing tier.
UPDATE aslan_core.api_key
   SET rate_overrides = jsonb_build_object('per_minute', 1200)
 WHERE key_id = '<uuid>';
```

Tier defaults are applied if `rate_overrides->>'<bucket>'` is NULL.

---

## 5. Common incidents

### 5a. Canary red

The Moat 2 canary lives at `scripts/canary_moat_2.py`; it runs every
5 minutes (see compose entry, §11) and writes status to
`aslan_core.feature_flags.value_text` for the synthetic flag
`MOAT_2_CANARY_STATUS`. Failing case ids land in
`aslan_core.feature_flags.notes` as JSON.

Triage:

```sql
-- 1. Current status + last failing cases.
SELECT flag_name, value_text, updated_at, notes
  FROM aslan_core.feature_flags
 WHERE flag_name = 'MOAT_2_CANARY_STATUS';

-- 2. Recent canary audit rows (the canary is itself an internal-tier
--    API key, so its requests show up in api_query_audit).
SELECT requested_at, endpoint, status_code, error_code, latency_ms
  FROM aslan_core.api_query_audit
 WHERE api_key_id = '<canary-key-uuid>'
 ORDER BY requested_at DESC
 LIMIT 20;
```

Re-run the canary manually against staging without persisting state
(useful for quick "did we fix it?" loops):

```powershell
ssh hetzner 'cd ~/aslan-core && docker compose run --rm --entrypoint "python scripts/canary_moat_2.py --no-persist" api'
```

If the canary is red on production but green on shadow, the prod
data is the source of regression — not the code. Bisect by re-running
each case individually:
`python scripts/canary_moat_2.py --case-id <id> --no-persist`.

### 5b. Slow queries (p99 > 100ms target)

```sql
-- Top 20 slowest research endpoints in the last hour.
SELECT endpoint,
       count(*)                          AS requests,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99_ms,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms
  FROM aslan_core.api_query_audit
 WHERE requested_at > NOW() - INTERVAL '1 hour'
   AND endpoint LIKE 'GET /v1/research/%'
 GROUP BY endpoint
 ORDER BY p99_ms DESC
 LIMIT 20;

-- pg_stat_statements: find the offending PIT function.
SELECT query, calls, mean_exec_time, max_exec_time
  FROM pg_stat_statements
 WHERE query LIKE '%_at(%'   -- PIT functions are named <table>_at
 ORDER BY mean_exec_time DESC
 LIMIT 20;

-- EXPLAIN one to confirm index usage on (entity_id, as_of).
EXPLAIN (ANALYZE, BUFFERS)
  SELECT * FROM ts.observation_at('2024-09-30T17:00:00Z'::timestamptz)
   WHERE series_id = 'BIST.GARAN.close';
-- Look for "Index Scan using ts_observation_series_id_ts_as_of_idx".
-- A Seq Scan or Bitmap Heap Scan over >100k rows is the bug.
```

If an index is missing on a freshly added bitemporal table, add a
forward-only migration; do NOT edit an applied one (per
`aslan-core/CLAUDE.md`).

### 5c. Audit log filling disk

Retention is 13 months (per D18; matches GDPR floor). The cron in
`infra/deploy/crontab` runs:

```sql
DELETE FROM aslan_core.api_query_audit
 WHERE requested_at < NOW() - INTERVAL '13 months';
```

If disk fills early (e.g. a stuck ingestion has produced an audit-log
storm), emergency truncate:

```sql
-- Emergency: drop everything older than 30 days.
-- ONLY in active incident; document the early truncation in
-- docs/incidents/<date>-audit-log-disk.md so the compliance team is
-- aware of the gap.
DELETE FROM aslan_core.api_query_audit
 WHERE requested_at < NOW() - INTERVAL '30 days';
VACUUM (VERBOSE, ANALYZE) aslan_core.api_query_audit;
```

GDPR Article 30 obligations require we keep the schema-level audit;
truncating is the lesser evil only when disk-full would crash the API.

---

## 6. Monitoring dashboards

Prometheus metrics (per SCOPE.md D25) on `:9001/metrics`:

- `bitemporal_api_request_total{endpoint,status,api_key_id}`
- `bitemporal_api_request_duration_seconds_bucket{endpoint,le}`
- `bitemporal_api_audit_log_inserts_total`
- `bitemporal_api_rate_limit_throttles_total{api_key_id}`
- `bitemporal_api_pit_query_duration_seconds_bucket{table,le}`
- `moat_2_canary_failures_total{case_id}`
- `moat_2_canary_last_success_timestamp_seconds`
- `bitemporal_api_feature_flag_state{flag,state}`
- `bitemporal_api_pii_access_total{key_id}`

Grafana dashboards (placeholders; not yet built):

- `bitemporal-api-overview` — RPS, error rate, p50/p95/p99 latency by endpoint.
- `bitemporal-api-pit-latency` — PIT function latency histograms broken
  out by table.
- `bitemporal-api-canary` — Moat 2 canary green/red over time.
- `bitemporal-api-audit-log` — insert rate vs retention runway.

---

## 7. Alerting thresholds

| Alert | Trigger | Page |
|---|---|---|
| `moat-2-canary-red` | `MOAT_2_CANARY_STATUS = 'red'` for >1 cycle | sidar (immediate) |
| `bitemporal-api-p99-high` | p99 > 100ms for 5 consecutive minutes | sidar (high) |
| `bitemporal-api-audit-drop` | audit-log insert rate drops > 50% from 1h baseline | sidar (high; suggests audit middleware broken) |
| `bitemporal-api-master-flag-flipped` | `BITEMPORAL_API_ENABLED` toggled outside change window | sidar (immediate) |
| `bitemporal-api-rate-limit-spike` | `bitemporal_api_rate_limit_throttles_total` rate-of-change > 100/min | sidar (med; possible abuse / runaway client) |
| `bitemporal-api-pii-access` | `bitemporal_api_pii_access_total` non-zero on a key not whitelisted | sidar (immediate; compliance) |

Wire via existing GlitchTip / alert routes; the prometheus scrape config is in `infra/observability/prometheus.yml` (TODO: not yet authored — track separately).

---

## 8. Kill-switch

If the API needs to be taken down immediately:

```sql
UPDATE aslan_core.feature_flags
   SET value_bool = false,
       updated_at = NOW(),
       notes      = COALESCE(notes,'') ||
                    E'\nKILL-SWITCH by <name> at ' || NOW()::text
 WHERE flag_name = 'BITEMPORAL_API_ENABLED';
```

Verify endpoints return 503:

```powershell
ssh hetzner 'curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8600/v1/research/observations'
# expect: 503
```

The kill-switch does NOT stop the FastAPI process — only the master
flag gate. Existing in-flight requests continue; new requests get 503
within ~milliseconds (flag fetch is cached for 1s in
`research_auth.py::get_feature_flags`).

To restore: set `value_bool = true` (§3).

---

## 9. Canary cron (compose profile)

The canary runs as a profile-gated service in
`infra/deploy/docker-compose.yml`. Start:

```powershell
ssh hetzner 'cd ~/aslan-core/infra/deploy && docker compose --profile bitemporal-canary up -d bitemporal-canary'
```

Stop:

```powershell
ssh hetzner 'cd ~/aslan-core/infra/deploy && docker compose --profile bitemporal-canary down bitemporal-canary'
```

Logs:

```powershell
ssh hetzner 'docker logs -f --tail 200 aslan-dashboard-bitemporal-canary-1'
```
