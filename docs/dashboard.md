# Dashboard runbook (v0.6.0)

The aslan-core dashboard is a read-only operator-facing FastHTML web
UI for the Aslan Terminal data plane. It surfaces outbox state,
stream lag, dead-letter rows, ingestion runs, document filings,
timeseries health, audit events (with PII truncation), and the
redaction registry — all behind the dedicated `aslan_dashboard`
PostgreSQL role created in migration 0020.

This document is the operator runbook. The full design contract lives
in `crawl/plans/aslan-core-v0.6.0-dashboard.md`.

## TL;DR

```
uv pip install -e '.[dashboard]'
alembic upgrade head
export ASLAN_DASHBOARD_DSN=postgresql://aslan_dashboard:<password>@localhost/aslan
export ASLAN_REDIS_URL=redis://localhost:6379/0
aslan dashboard serve
# → http://127.0.0.1:8585/
```

The dashboard refuses to bind to a non-loopback interface unless you
pass `--i-know-this-is-unsafe`. See *Reverse-proxy deployment* below
before exposing it on a public address.

## What the dashboard is NOT

* **Not a write surface.** Every HTTP method except `GET` returns 405.
  The PostgreSQL role lacks `INSERT`/`UPDATE`/`DELETE` on every table
  including `audit.events`. Mutations (deadletter redrive, manual
  XACK, etc.) ship in v0.7.x once `aslan-service` provides
  authenticated per-request operator identity.
* **Not compliance evidence in v0.6.0.** The `/audit` and
  `/redactions` pages render a standing banner reading
  `Network-edge logging only — not compliance evidence`. Per-view
  attribution requires authenticated identity, which v0.6.0 does not
  ship. For SOC 2 / GDPR-grade attestations, run behind a
  reverse-proxy with mTLS or SSO header injection plus structured
  access logs (see *Reverse-proxy deployment*).
* **Not a payload viewer.** Forbidden columns
  (`outbox.payload`, `filing.body_text`, `redaction_registry.redacted_payload`,
  `audit.events.metadata`, `last_error` raw text, etc.) are revoked
  from the dashboard role at the column-allowlist GRANT level.
  Operators inspect payload bytes via `aslan deadletter inspect`,
  `aslan ingestion show`, or `aslan audit show` — those CLIs run
  under the OS user as the audit actor.

## Install

```
uv pip install -e '.[dashboard]'
```

The `[dashboard]` extra pulls `python-fasthtml` and
`uvicorn[standard]`. Without it, `from aslan_core import dashboard`
still imports cleanly (signatures only) so unit-only consumers stay
slim.

## Database setup

The dashboard logs in as `aslan_dashboard`, a dedicated role created
in migration 0020 with:

* Column-allowlist `SELECT` GRANTs on every table (no forbidden
  columns are reachable).
* `default_transaction_read_only = on` at role level — every
  transaction is read-only without an explicit `BEGIN READ ONLY`.
* `REVOKE INSERT/UPDATE/DELETE/TRUNCATE` on every table.

Run migrations to install the role:

```
alembic upgrade head
```

The dev fallback password (`DEV_ONLY_REPLACE_ME`) is installed only
when `ASLAN_DASHBOARD_PASSWORD` is unset and `ASLAN_ENV=dev`. In
production set `ASLAN_DASHBOARD_PASSWORD` to a real secret before
running the migration.

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `ASLAN_DASHBOARD_DSN` | yes | — | `postgresql://aslan_dashboard:<password>@<host>/<db>` |
| `ASLAN_REDIS_URL` | no | `redis://127.0.0.1:6379/0` | shared with the rest of aslan-core |

Without `ASLAN_DASHBOARD_DSN` the CLI exits with a usage error
naming migration 0020 — turn the failure into a self-service
recovery.

## CLI

```
aslan dashboard serve [--host HOST] [--port PORT] [--i-know-this-is-unsafe]
```

* `--host` defaults to `127.0.0.1`. Loopback hosts (`127.0.0.1`,
  `localhost`, `::1`) are accepted without flags.
* Non-loopback hosts (`0.0.0.0`, public IPs) require
  `--i-know-this-is-unsafe`. The flag is the operator's explicit
  acknowledgement that a fronting reverse-proxy is in place — see
  the next section.
* No auto-reload flag. uvicorn's `--reload` requires an
  import-string + per-worker startup hook plumbing that v0.6.0
  does not ship; restart the process manually during development.

## Localhost / SSH-tunnel deployment

The default deployment shape. Bind to `127.0.0.1`, ssh-tunnel to the
host, point a browser at the loopback port. The compliance posture
is "engineering ops use during incident response and deploy
verification" — the network edge (SSH access logs, VPN logs) covers
who-reached-the-host. The dashboard makes no per-view attribution
claim.

```
ssh -L 8585:127.0.0.1:8585 aslan-prod-host
aslan dashboard serve  # on the remote host
# locally: open http://127.0.0.1:8585/
```

## Reverse-proxy deployment

For deployments that need stronger compliance posture (SOC 2 /
GDPR-grade), front the dashboard with a reverse-proxy that:

1. **Authenticates the principal** via mTLS client certificate
   subject OR an SSO proxy header (e.g. Cloudflare Access JWT,
   Okta header).
2. **Logs access** as a structured line containing the path
   (sanitized — no body, no full query string for sensitive
   routes), the authenticated principal, the timestamp, and the
   response status.
3. **Retains** logs for ≥ 90 days with integrity-protected shipping
   (e.g. signed log chain, append-only S3 bucket with object lock).
4. **Forwards** the principal in a header the dashboard can ignore
   for now (audit emission lands in v0.7.x).

Then run the dashboard with `--i-know-this-is-unsafe` (the bypass
flag is the operator's signal that the proxy story is complete):

```
aslan dashboard serve --host 0.0.0.0 --i-know-this-is-unsafe
```

Without this proxy story the deployment is "not for compliance-
sensitive use" and should remain on loopback.

## Pages

| Path | Purpose | Notes |
|---|---|---|
| `/` | Overview cards | outbox pending, deadletter total, ingestion last-24h, audit/min, redaction registry size |
| `/outbox` | recent outbox rows | last_error_kind only; raw last_error revoked |
| `/streams` | per-stream xlen + last-entry-age | Redis-driven; circuit breaker visible |
| `/ingestion` | recent ingestion runs | last_error_kind only |
| `/documents` | recent filings | body_text, summary_text NEVER rendered |
| `/timeseries` | series catalog with last-observation timestamps | |
| `/deadletter` | deadletter_log + Redis index state | redis_state ∈ {present, trimmed, missing_index, unknown} |
| `/audit` | audit.events with truncated client_ip + metadata key count | **standing compliance banner** |
| `/redactions` | redaction_registry with hashes only | **standing compliance banner** |
| `/metrics` | Prometheus exposition | request counter + duration; closed-enum labels |
| `/static/dashboard.css` | stylesheet | bundled |
| `/static/htmx.min.js` | vendored htmx 1.9.12 (SRI-pinned) | bundled |
| `/static/favicon.ico` | favicon | bundled |

A typo on a page path returns a static 404 page that does NOT echo
the requested URL. An unhandled exception returns a 500 page with
only an `incident_id` (UUID); the exception text is in the structured
log keyed on the same id.

## Inspecting forbidden bytes

The dashboard refuses to render forbidden columns. To inspect a
payload, traceback, or metadata blob:

```
aslan deadletter inspect <failure_id>      # streams.deadletter_log
aslan ingestion show <ingestion_run_id>    # src.ingestion_run
aslan audit show <event_id>                # audit.events
```

These CLIs run under the operator's OS user; the audit actor is
`cli:<user>@<host>` — a durable identity that survives the dashboard
process's lack of authenticated identity.

## When the Redis circuit breaker trips

The dashboard's Redis probes share a per-process circuit breaker:
5 errors in 60 seconds opens the breaker for 30 seconds. While open,
every probe short-circuits to `None` and pages render `unknown` cells
where Redis data would have appeared.

The `/streams` page surfaces the breaker state explicitly
(`Redis circuit: open` vs `closed`). When you see `open`:

1. Verify Redis is reachable: `redis-cli -u "$ASLAN_REDIS_URL" PING`.
2. Check for network partition between the dashboard host and Redis.
3. Wait 30 seconds; the breaker re-tries on the next probe.
4. If the breaker stays open, restart the dashboard process —
   breaker state is in-process.

## Metrics + alerting

The `/metrics` endpoint exposes:

* `aslan_dashboard_requests_total{path, status}` — counter labeled
  by closed-enum `path` and `status`. `/audit` and `/redactions`
  collapse to `path="<sensitive>"` so a metric observer cannot
  distinguish the two surfaces. `/static/*` collapses to
  `"/static"`. Any other path collapses to `"<other>"`. Status
  collapses to `"<other>"` outside `{200, 404, 405, 500}`.
* `aslan_dashboard_request_duration_seconds` — histogram with NO
  labels. Per-route timing is a side-channel; alerting on p99
  latency stays actionable globally without leaking which surface
  is slow.

A spike in `status="500"` is the operator's earliest signal of a
dashboard regression — page handlers should never reach that path
under normal traffic.

## Static assets + SRI

The dashboard ships vendored htmx 1.9.12 with the SHA-256 hash
recorded in `src/aslan_core/dashboard/assets/htmx.min.js.sha256`.
The `<script>` tag in the base template carries a matching
`integrity` attribute so browsers refuse to execute a tampered
file. App startup verifies the sidecar against the file bytes; a
mismatch raises at module-import time rather than serving a
hash-mismatched script.

To re-vendor:

```
curl -fsSL https://unpkg.com/htmx.org@<version>/dist/htmx.min.js \
    -o src/aslan_core/dashboard/assets/htmx.min.js
shasum -a 256 src/aslan_core/dashboard/assets/htmx.min.js \
    | awk '{print $1}' \
    > src/aslan_core/dashboard/assets/htmx.min.js.sha256
```

Both files MUST be updated together or app startup fails.
