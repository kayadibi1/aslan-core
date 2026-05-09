# DQ M1 emitter wiring handoff (cross-repo)

The full handoff lives at the workspace root:
`../../docs/superpowers/handoffs/2026-05-09-dq-m1-emitter-wiring.md`
(from this repo's directory) — i.e. one level above the
`aslan-core-dq-m0` repo, in the workspace's shared `docs/superpowers/handoffs/`.

This file is a stub so the aslan-core repo's git history records that
the handoff exists at workspace level. The workspace handoff is the
canonical source for per-repo wiring guidance.

## Summary

M1 of the dq subsystem is **complete on the aslan-core side**. Six
sibling repos (`crawl`, `aslan-bist-puller`, `aslan-evds-puller`,
`aslan-mkk-puller`, `aslan-tefas-puller`, `aslan-event-extractor`)
need their puller / extractor entry points wrapped with
`dq.sync_log.run(...)` + `dq.validation.check(...)` so the M1
dashboard renders populated heatmap cells.

## What aslan-core delivers (M1)

* `aslan_core.dq.probes` package (5 source probes + Protocol)
* `aslan_core.dq.recency.observe()` persistence helper
* `aslan-core audit recency-sweep` CLI (writes `audit.recency_observation`)
* `aslan-core audit coverage-snapshot` CLI (writes `audit.coverage_snapshot`)
* `/dq/overview` heatmap (5x4 sources x dimensions)
* `/dq/recency` lag time-series (1h / 24h / 7d / 30d aggregates)
* `/dq/coverage` tabular view (per source x dimension)
* Migration 0057 — `aslan_dashboard` SELECT grants on the nine
  audit.* dq tables

## What the sibling repos need (deferred)

The full per-repo wiring guidance (entry points, file:line, source-
specific edge cases) is in the workspace handoff. Summary:

| Repo | Entry point | Source name |
| --- | --- | --- |
| crawl | `src/kap/cli/sync_disclosures.py:81` | kap |
| aslan-bist-puller | `src/bist_puller/cli/pull.py:14` | bist |
| aslan-evds-puller | `src/evds/cli/pull.py:117` | evds |
| aslan-tefas-puller | `src/tefas_puller/cli/pull.py:42` | tefas |
| aslan-mkk-puller | `src/mkk_puller/cli/pull.py:32` | mkk |
| aslan-event-extractor | `src/aslan_event_extractor/cli/extract.py` | event-extractor |

## Pending follow-ups

* M1.1 — replace placeholder upstream probes in
  `src/aslan_core/dq/probes/{kap,bist,tefas,mkk}.py` with real
  upstream queries (HTTP-poll KAP, ref.calendar_tr for BIST holiday
  gating, per-fund rolling-90d-median for TEFAS, MKK API).
* M2 — **shipped on this branch**. Spot-check labelling workflow
  is live: migration 0058 (`audit.spot_check_sample` +
  `audit.spot_check_result`), `aslan_core.dq.spot_check` module,
  `aslan-core audit spot-check-draw` CLI (Mon 06:00 UTC cron),
  `/dq/spot-check` pending queue + per-sample labelling form
  (GET + POST). Best-effort mirror into `agg.filing_event_label`
  for KAP samples. Workspace handoff carries the operating-cycle
  detail under `## Spot-check labelling workflow (M2)`.

## M3 alert deployment

M3 of the dq subsystem ships the alert-dispatcher cron + the three
alert sinks (GlitchTip / email / Slack). Alembic head is `0059`
(`20260509_1506_0059_dq_severity_rule_seed.py`) which seeds the nine
canonical rules from spec §10.3 into `audit.severity_rule`. The
dispatcher itself is `aslan_core.dq.alert_dispatch`; sinks live under
`aslan_core.dq.sinks/{glitchtip,email,slack}.py`.

### Required env vars (Hetzner production)

* `ASLAN_AUDIT_GLITCHTIP_DSN` — Sentry-format DSN pointing at the
  existing GlitchTip on Hetzner :8800 (the same instance the legacy
  `ASLAN_SENTRY_DSN` targets — but use a separate project so audit
  alerts don't pollute the application error stream).
* `ASLAN_AUDIT_SMTP_URL` — `smtp://user:pass@host:port` or
  `smtps://...`. Workspace doesn't have an SMTP relay yet; suggested
  options:
  * **Resend** (`smtps://resend:<api-key>@smtp.resend.com:465`) — €20/mo
    for 50k messages, no DMARC/SPF setup required since they manage the
    `resend.dev` send domain.
  * **AWS SES** (`smtps://AKIA…:secret@email-smtp.eu-central-1.amazonaws.com:465`)
    — €0.10 / 1k messages, but requires DKIM/SPF/DMARC on the send
    domain. Cheaper at scale but more setup work; recommended once we
    have a stable from-address.
* `ASLAN_AUDIT_EMAIL_TO` — comma-separated; default ops alias
  `audit@aslanterminal.tr` (still to be created) plus `sidar@gmail.com`
  during the M3 rollout window.
* `ASLAN_AUDIT_SLACK_WEBHOOK_URL` — Slack incoming-webhook URL.
  Workspace doesn't have a `#audit` channel yet; create via Slack
  admin → Apps → Incoming Webhooks → bind to a new private
  `#aslan-audit` channel.

Each is **optional**. A sink whose env var is unset surfaces in
`audit.alert_dispatch` as `status='suppressed'` with a
`_dispatch_suppressed_reason` field on the row payload — the
dispatcher never raises into the cron loop on missing config.

### Cron schedule (production)

```
* * * * * aslan-core audit alert-dispatch
```

Once per minute. The dispatcher is idempotent — re-firing the same
condition twice inside the same minute is a no-op via the
`ad_throttle_dedup` unique index on
`(rule_name, sink, content_hash, date_trunc('minute', fired_at))`.

### Smoke test (post-deploy)

```
aslan-core audit test-alert --severity critical --sink all
```

Emits one synthetic `audit.event(event_type='test_alert_emitted')`
and pushes a synthetic payload through every configured sink. Each
sink prints its delivery status (`delivered` / `suppressed` /
`failed`) to stdout/stderr so the operator can confirm wiring before
the next real condition fires.

`aslan-core audit test-alert --sink glitchtip` is the canonical
post-deploy gate: a configured GlitchTip sink that returns 2xx
proves the DSN is correct, the project is reachable from the
Hetzner host, and the auth header is well-formed without waiting
for an organic SLA breach.

### Rule scope on this branch

Of the nine seeded rules, **six** target tables that exist on this
branch and fire today: `recency_sla_breach`, `recency_sla_breach_2x`,
`validation_pass_rate_below_95/90`, `coverage_below_target`,
`coverage_below_90pct`, plus `weekly_scorecard` once the M6 cron
lands.

`bloomberg_loses_field` (M4) and `regression_flag_critical_entity`
(M5) are seeded but skipped at runtime — the dispatcher detects the
missing `audit.bloomberg_comparison_cell` / `audit.regression_flag`
tables via `information_schema.tables` and emits one
`audit.event(event_type='alert_rule_skipped')` per sweep so the gap
is observable on /dq.

### Operational runbook

* **Pending row stuck in `status='failed'`** — inspect
  `payload->>'_dispatch_error'`. For 5xx from GlitchTip / Slack,
  rerun `aslan-core audit alert-dispatch --dispatch-only` after the
  upstream recovers; the dispatcher requires fresh rows (failed is
  terminal), so the next predicate firing will land a new row at the
  next minute boundary.
* **All rows landing as `status='suppressed'`** — the relevant env
  var is unset; check the systemd unit's environment file. The
  `_dispatch_suppressed_reason` payload key carries the exact text
  from `SinkNotConfigured`.
* **Alert fatigue (re-firing every minute)** — bump the rule's
  `throttle_seconds` via `UPDATE audit.severity_rule SET
  throttle_seconds = … WHERE rule_name = …;` (the table has the
  `severity_rule_change_audit` trigger, so the change shows up in
  `audit.event(event_type='severity_rule_changed')`).

