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

## M4 Bloomberg-comparison rotation

M4 ships the Bloomberg-vs-Aslan quarterly comparison framework. Alembic
head moves to `0060` (`20260509_1507_0060_dq_bloomberg_comparison.py`),
adding `audit.bloomberg_comparison_run` + `audit.bloomberg_comparison_cell`.
The framework lives in `aslan_core.dq.bloomberg`; the dashboard surface
is `/dq/bloomberg` (overview + per-cell entry form + history) plus
`/dq/bloomberg/runs/{run_id}` (per-run drill-down). Five new CLIs land
under `aslan-core audit bloomberg-*`.

### Cell catalogue (60 cells per quarter)

5 anchor BIST entities × 12 fields each:

* Anchor entities: `AKBNK`, `ASELS`, `GARAN`, `KCHOL`, `TUPRS` —
  large issuers with quarterly disclosures, dividend histories, and
  capital actions in the KAP corpus, so each cell has a meaningful
  Bloomberg counterpart.
* Fields:
  * `revenue_q-1` … `revenue_q-4` (4 latest quarterly revenues from
    `ts.canonical_financial`, code `is.revenue`)
  * `net_income_q-1` … `net_income_q-4` (4 latest, code `is.net_income`)
  * `latest_dividend_amount` (placeholder; v1 wiring deferred to M4.1)
  * `latest_capital_action` (placeholder)
  * `filing_lag_p95_30d` (audit-derived: p95 of
    `audit.recency_observation.lag_seconds` for KAP over 30d)
  * `material_event_field_count` (Aslan-only signal; placeholder)

Placeholder samplers emit one
`audit.event(event_type='bloomberg_sampler_placeholder')` per call so
the gap is observable on /dq.

### Quarterly schedule

The aslan-team operates a quarterly rotation:

* **First Mon of each quarter** — `aslan-core audit bloomberg-open-quarter`.
  Idempotent: a second invocation for the same quarter is a no-op.
* **Every Mon thereafter** — sidar enters Bloomberg values via
  `/dq/bloomberg`. The page renders one row per cell with NULL
  `bloomberg_value` and an inline POST form.
* **Nightly** — `aslan-core audit bloomberg-sample` runs the auto-sampler.
  Sample cron line:
  ```
  5 1 * * * aslan-core audit bloomberg-sample
  ```
  Drains every cell with `aslan_value IS NULL` across all open runs,
  populating `aslan_value` + `variance_pct` + `aslan_advantage`.
* **Last Fri of each quarter** — `aslan-core audit bloomberg-close-quarter
  --quarter <YYYYQn>`. Refuses with `QuarterNotReadyError` if any
  `bloomberg_value` is still NULL (spec §17 R2). Once all 60 cells are
  filled, the close stamps `closed_at` on the run, and the run flows
  into the `claim_check` aggregate.

### Reminder ping mechanism

Once a Bloomberg-comparison run has been open for more than a week
with NULL `bloomberg_value` cells remaining, the operator gets a
weekly Slack ping via:

```
0 9 * * MON aslan-core audit bloomberg-reminder
```

The reminder always lands a row in `audit.event(event_type='bloomberg_reminder')`
even when no Slack sink is configured — so the gap is auditable on
/audit even before Slack is wired. Reminder Slack messages reuse the
M3 `SlackSink` machinery; if `ASLAN_AUDIT_SLACK_WEBHOOK_URL` is unset
the reminder counts as suppressed (same contract as the alert
dispatcher).

### `aslan-event-extractor/docs/comparisons/bloomberg.md`

The markdown comparison report at the workspace path
`aslan-event-extractor/docs/comparisons/bloomberg.md` is **generated**,
not hand-written. It is overwritten on each invocation of:

```
aslan-core audit bloomberg-render
```

The render CLI auto-creates the parent directory if missing, so a
fresh checkout of `aslan-event-extractor` does not need to pre-create
`docs/comparisons/`. **Do not edit the file by hand** — the next
render run will clobber any manual changes. The intended cadence is
"after every closed quarter" (a one-shot CLI invocation, not a cron).

### Per-PR claim defence

Per workspace `CLAUDE.md` §2 ("Does this meet or exceed Bloomberg's TR
coverage on the relevant dimension?"), every PR that claims an Aslan
advantage on a comparison field must paste the output of:

```
aslan-core audit bloomberg-claim-check --field <FIELD>
```

into the PR description. The output is a markdown block listing the
most-recent CLOSED run's per-entity verdict for that field. If no
closed run exists yet, the CLI prints a short prompt explaining that
the claim cannot yet be defended; in that case the PR author must
either open + close a quarter first, or downgrade the claim to "we
expect to beat Bloomberg once the first comparison closes."

### Dashboard surface (M4)

* `GET /dq/bloomberg` — latest run summary (wins/ties/loses headline
  counts, NULL-cell count), the 60-cell grid grouped by entity with
  inline entry forms on NULL bloomberg cells, and a History block
  listing past closed runs with click-through.
* `GET /dq/bloomberg/runs/{run_id}` — per-run drill-down. Always
  read-only (closed runs cannot be re-edited).
* `POST /dq/bloomberg/cells/{cell_id}` — manual-entry submit. Mirrors
  the `/dq/spot-check/{sample_id}` pattern: column-level UPDATE on
  `bloomberg_value` is granted to `audit_admin` only (migration 0060);
  the dashboard role itself remains SELECT-only. Production wires
  sidar's authenticated session against the `audit_admin` role.

### Sources of truth

* Module: `src/aslan_core/dq/bloomberg.py` — public API
  (`open_quarter`, `record_bloomberg_value`, `record_aslan_value`,
  `close_quarter`, `claim_check`, `render_markdown`).
* CLI: `src/aslan_core/cli/dq.py` — five new commands listed above.
* Dashboard: `src/aslan_core/dashboard/pages/dq_bloomberg.py` +
  `_bloomberg_*` query helpers in `src/aslan_core/dashboard/queries.py`.
* Migration: `0060_dq_bloomberg_comparison.py` — table + index +
  GRANT layout.


## M5 cross-source + regression detection

Spec §7.3 (cross-source consistency) + §7.4 (regression detection
v1) + autonomy directive on this branch (regression v2 / NG5
promoted to in-scope, post-processing of v1 inside the same cron).

### Cron schedule

Two new entries in the production crontab:

```
0 2 * * * aslan-core audit cross-source-consistency
0 3 * * * aslan-core audit regression-detect
```

Both run nightly. The two crons are independent — cross-source
writes to `audit.validation_failure`; regression-detect writes to
`audit.regression_flag`. They share no table-level state and can run
in either order.

### Cross-source rules (§7.3)

Six rules under `src/aslan_core/dq/cross_source.py`. Each is a
coroutine returning `list[ValidationFailure]`; each persists every
firing to `audit.validation_failure` for durable audit.

| Rule | Severity | Source tables |
|---|---|---|
| `xs_tefas_holding_dangling_entity` | warn | `tefas.fund_holding`, `ref.entity` |
| `xs_bist_ticker_kap_issuer` | warn | `bist.security`, `ref.identifier` |
| `xs_mkk_kap_capital_action_corr` | warn | `mkk.capital_action`, `kap.disclosures` |
| `xs_tefas_nav_holdings_recon` | error | `tefas.fund_nav`, `tefas.fund_holding`, `bist.daily_ohlcv` |
| `xs_evds_observation_calendar` | error | `audit.evds_release_calendar`, `evds.observation` |
| `xs_kap_filing_count_recon` | info | `kap.disclosures` (placeholder; see §M5.1 below) |

When a rule's required source tables are absent on this branch /
environment the rule emits a single `xs_rule_skipped` audit event and
returns `[]` instead of raising. The cron loop continues with the
next rule. The `/dq/validation` page surfaces these skips at the
bottom so operators see when a rule lapsed for table-availability
reasons.

### Regression detection v1 (§7.4)

`src/aslan_core/dq/regression_detect.py` runs the curated metric
list against every BIST roster entity (resolved via
`ref.identifier(namespace='bist_ticker')`):

| Metric | canonical_code | Period | Threshold |
|---|---|---|---|
| revenue | `is.revenue` | QoQ | 25% |
| net_income | `is.net_income` | QoQ | 40% |
| total_assets | `bs.total_assets` | QoQ | 15% |
| debt_to_equity | `ratio.debt_to_equity` | QoQ | 30% |
| pe | `ratio.pe` | DoD | 20% |
| roe | `ratio.roe` | QoQ | 30% |

The three ratio metrics (`debt_to_equity`, `pe`, `roe`) are flagged
`wired=False` until the financial canonicaliser ships a ratio-
projection step (M2.1 patch). The detector emits a single
`regression_metric_unwired` audit event per unwired metric per run
and skips it.

### Regression detection v2 — KAP filing correlation (NG5 promoted)

After v1 inserts new flags, the same cron runs `correlate_v2`. For
each new flag, it queries `kap.disclosures` for filings on the
flag's entity in the window **`[detected_at - 7 days, detected_at + 1 day]`**.
A filing of category in `{material_event, capital_action, dividend}`
auto-dismisses the flag with:

* `status='dismissed'`
* `reviewer='cli:audit-regression-detect'`
* `review_note='auto-dismissed: justified by KAP filing <disclosure_id>'`
* an additional `audit.event(event_type='regression_auto_dismissed')`
  carrying the matched disclosure_id, category, and window bounds

The 7d back / 1d forward window is justified by KAP filing data: a
material event published up to a week before the period close that
moves the metric is the canonical justification for the move; an
event up to 1 day after the detection covers same-day disclosures
that land on the cron's morning run. Tighter windows produced too
many false-positive open flags during shadow runs; wider windows
auto-dismissed legitimate data-issue regressions.

### Once `aslan-event-extractor` M3 lands

The v2 correlation can become more selective. Today's heuristic
fires on any filing in `{material_event, capital_action, dividend}`;
once M3's extracted `material_change` boolean is on
`agg.filing_event`, v2 should additionally filter to filings with
`material_change=true`. That tightens the auto-dismiss to
**actually-material** filings, making the open queue more useful
for sidar's review pass.

### Curated entity roster — placeholder

The detector currently uses **every** entity with a current
`ref.identifier(namespace='bist_ticker')` row, capped at 50. The
spec calls for a curated top-50 by liquidity; **sidar curation of
the actual list is deferred**. When the curated list lands, replace
`_SELECT_TOP_BIST_ENTITIES` in
`src/aslan_core/dq/regression_detect.py` with a JOIN against the
new `audit.regression_top50` (or equivalent) table. The placeholder
is documented inline.

### Migration 0061

`audit.regression_flag` per spec §5.7 — append-only, with `status`
the lone mutable column. GRANTs:

* `audit_writer` — INSERT only (the detector cron runs as audit_writer)
* `audit_admin` — UPDATE on `status` / `reviewer` / `reviewed_at` /
  `review_note` (the dashboard review POST + the v2 auto-dismiss path
  both run under audit_admin since both are review actions)
* `audit_reader` — SELECT
* `aslan_dashboard` — SELECT (the dashboard process is read-only;
  the review POST goes through a write-capable role per the
  spot-check / Bloomberg pattern)

### Dashboard `/dq/validation`

Replaces the M0 stub. Three sections:

1. Top — per-rule failure rate over 7d (groups in-line validators
   and `xs_*` cross-source rules into one table).
2. Middle — open `audit.regression_flag` rows with click-through
   review form (status: dismiss / confirm bug / mark reviewed).
3. Bottom — recent `xs_rule_skipped` events.

POST handler: `/dq/validation/regression/{flag_id}` accepts the
review form and 303-redirects back. Bound character classes /
lengths on reviewer + review_note, status enum-checked. v2
auto-dismissed flags are NOT shown in the open-queue (they leave
`status='dismissed'` and live in `audit.event(event_type='regression_auto_dismissed')`).

### Sources of truth

* Modules:
  * `src/aslan_core/dq/regression.py` — `flag` / `set_status` /
    `pending_flags` / `get_flag`.
  * `src/aslan_core/dq/cross_source.py` — 6 rules + `run_all` driver.
  * `src/aslan_core/dq/regression_detect.py` — `detect_v1`,
    `correlate_v2`, `detect_and_correlate`.
* CLI: `src/aslan_core/cli/dq.py` — `cross-source-consistency` and
  `regression-detect` subcommands.
* Dashboard: `src/aslan_core/dashboard/pages/dq_validation.py` +
  `dq_validation` query helper in
  `src/aslan_core/dashboard/queries.py`.
* Migration: `0061_dq_regression_flag.py`.
* Tests:
  * `tests/integration/test_migration_0061_regression_flag.py`
  * `tests/integration/dq/test_regression_roundtrip.py`
  * `tests/integration/dq/test_cross_source_rules.py`
  * `tests/integration/dq/test_cross_source_cli.py`
  * `tests/integration/dq/test_regression_detect.py`
  * `tests/integration/dashboard/test_dq_validation.py`

### M5.1 follow-ups (deferred, but not blocking M5)

* **`xs_kap_filing_count_recon`** — wire the upstream KAP listing
  HTTP query and replace the trailing-7d-mean self-compare with a
  real upstream-vs-DB diff. The placeholder emits a
  `kap_api_count_unimplemented` event per run so the gap is visible.
* **Trading-day calendar for `xs_mkk_kap_capital_action_corr`** —
  v1 uses ±5 calendar days as a Mon-Fri ±3-trading-day approximation.
  Once `ref.calendar_tr` is populated, swap the SQL window to a real
  trading-day calculation.
* **Curated top-50 entity roster** — sidar curation pending. See
  the inline TODO in `regression_detect._SELECT_TOP_BIST_ENTITIES`.


