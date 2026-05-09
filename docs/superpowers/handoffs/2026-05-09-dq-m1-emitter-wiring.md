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


## M6 weekly scorecard

M6 ships the weekly DQ scorecard + email digest end-to-end. Sunday
23:55 UTC the `audit-scorecard` cron computes ten metrics aggregating
from the `audit.*` tables for the trailing 7 days, persists each as a
row in `audit.scorecard_snapshot`, and emits
`audit.event(event_type='scorecard_generated')` carrying the rendered
HTML body. The `weekly_scorecard` severity rule (seeded in 0059) picks
that event up on the next 60s alert-dispatch sweep and emails the
recipient list.

### Cron schedule

```cron
55 23  *   *   0   /usr/local/bin/aslan-core audit scorecard
```

Spec §13. Sunday 23:55 UTC. The cron summarises the just-ended week
(Mon-Sun ending today). Re-run for a specific past week:

```bash
aslan-core audit scorecard --week-start 2026-04-27
```

`--week-start` must be a Monday (UTC). The UPSERT
(`ON CONFLICT (week_start, metric_name) DO UPDATE`) keeps the
recompute idempotent — `recorded_at` stays at the original insert
time; `actual` / `status` / `notes` get overwritten in place.

### Recipient list configuration

The dispatcher's email sink reads the recipient list from
`ASLAN_AUDIT_EMAIL_TO` (comma-separated). The SMTP relay URL comes
from `ASLAN_AUDIT_SMTP_URL` (`smtp://user:pw@host:port` or
`smtps://...`). Both are settings in `aslan_core.config.Settings`
and the corresponding env vars are:

```bash
ASLAN_AUDIT_SMTP_URL="smtps://alerts:secret@smtp.relay:465"
ASLAN_AUDIT_EMAIL_TO="sidar@aslan.example,ops@aslan.example"
```

### The ten metrics

| metric_name | source | thresholds (pass / warn / fail) |
| --- | --- | --- |
| `kap_recency_p95` | `audit.recency_observation` p95 (kap, 7d) | ≤300s / ≤600s / >600s |
| `kap_recency_p99` | `audit.recency_observation` p99 (kap, 7d) | ≤1800s / ≤3000s / >3000s |
| `evds_freshness_pct` | `audit.evds_release_calendar` join `ts.observation` | ≥95% / ≥90% / <90% |
| `kap_filings_today_coverage` | latest `audit.coverage_snapshot` (kap/filings_today) | ≥99% / ≥95% / <95% |
| `bist_entity_coverage` | latest `audit.coverage_snapshot` (bist/entity) | =100% / ≥99% / <99% |
| `validation_pass_rate_kap` | `audit.validation_failure` (kap, 7d) / recency-obs proxy | ≥99% / ≥97% / <97% |
| `spot_check_completion_rate_4w` | `audit.spot_check_sample` labelled ratio (28d) | ≥90% / ≥80% / <80% |
| `bloomberg_wins_count` | latest CLOSED `audit.bloomberg_comparison_run` cells | ≥30 / ≥20 / <20 |
| `regression_flag_open_count` | `audit.regression_flag` status='open' | ≤5 / ≤10 / >10 |
| `cross_source_rule_clean_count` | `audit.validation_failure` xs_* rules clean (7d) | ≥5 / ≥4 / <4 |

Tables not deployed on a given branch (e.g. `audit.bloomberg_comparison_cell`
on a v1 puller-only branch) return a row with `status='warn'` and a
notes string explaining the gap rather than crashing the cron.

### Email body — rich-body mode on the email sink

The cron stuffs `subject` + `body_html` + `body_text` into the
`scorecard_generated` event payload. The dispatcher's
`weekly_scorecard` predicate query (in
`aslan_core.dq.alert_dispatch`) projects the full payload onto the
`audit.alert_dispatch.payload` column. `EmailSink.deliver` detects
the `body_html` key and switches to a multipart/alternative message:

* **Subject** — `payload['subject']` (overrides the generic
  `[ASLAN AUDIT] [info] weekly_scorecard`).
* **text/plain** — `payload['body_text']` (a monospace dump from
  `dq.scorecard.render_text_fallback`) so terminal mail clients still
  read the digest cleanly.
* **text/html** — `payload['body_html']` (the colour-coded HTML
  table from `dq.scorecard.render_email`).

Existing M3-era rules (recency_sla_breach, coverage_below_target, …)
have no `body_html` in their payload, so the legacy generic JSON-dump
body kicks in unchanged.

### Dashboard /dq/scorecard

* **GET /dq/scorecard** — current-week metrics + history roll-up +
  Email-preview / Export-HTML action links.
* **GET /dq/scorecard/email** — renders the latest scorecard's email
  body inline. Pulls from
  `audit.event(event_type='scorecard_generated').payload.body_html`;
  falls back to live-rendering when no event exists yet.
* **GET /dq/scorecard/export** — downloads a standalone HTML file
  named `aslan-scorecard-YYYY-MM-DD.html` for the current week.

### PDF export — deferred to M6.1

Real PDF rendering (WeasyPrint or reportlab) is deferred. The Export
button serves HTML; the operator's browser does the print-to-PDF step.
Documented inline in `dq.scorecard.render_html`'s docstring + the
dashboard footer; ticket the M6.1 follow-up alongside the next `[obs]`
extras refresh.

### Migration 0062

`audit.scorecard_snapshot` per spec §5.10. PK
`(week_start, metric_name)` enables idempotent UPSERT. GRANTs:

* `audit_writer` — INSERT + UPDATE (UPSERT requires both); the cron
  runs as audit_writer.
* `audit_reader` — SELECT.
* `aslan_dashboard` — SELECT (page is read-only).

### Sources of truth

* Module: `src/aslan_core/dq/scorecard.py` — `compute` / `write` /
  `render_email` / `render_html` / `render_text_fallback` /
  `latest_week_start`.
* CLI: `src/aslan_core/cli/dq.py` — `audit scorecard` subcommand.
* Email sink: `src/aslan_core/dq/sinks/email.py` — rich-body mode.
* Dashboard: `src/aslan_core/dashboard/pages/dq_scorecard.py` +
  `dq_scorecard` / `dq_scorecard_latest_email_body` query helpers
  in `src/aslan_core/dashboard/queries.py`.
* Migration: `0062_dq_scorecard_snapshot.py`.
* Tests:
  * `tests/integration/test_migration_0062_scorecard_snapshot.py`
  * `tests/integration/dq/test_scorecard_cli.py`
  * `tests/integration/dashboard/test_dq_scorecard.py`
  * `tests/unit/dq/test_scorecard_render.py`
  * Email rich-body coverage in `tests/unit/dq/test_sinks.py`


## NG1 Public status page deployment

NG1 surfaces a public, no-auth `/status` page rendering the
per-source freshness + coverage roll-up from `audit.recency_observation`
+ `audit.coverage_snapshot`. Sibling to the internal dashboard
(`aslan_core.dashboard`); reuses the same FastHTML stack but with a
separate process, separate port, separate (least-priv) DB role.

### Run command

```
aslan public-status serve --port 8081 --host 127.0.0.1
```

* Reads `ASLAN_PUBLIC_STATUS_DSN` for the `public_status_reader`
  role. Without it the CLI exits with a usage error pointing at
  migration 0063.
* Loopback by default. Non-loopback bind requires
  `--i-know-this-is-unsafe` — the page is intended to be fronted by
  a CDN / reverse proxy; binding directly to a public interface
  skips the rate limiting + structured access logging the fronting
  layer would supply.

### Recommended deployment shape

* Dedicated container with a read-only DB user
  (`public_status_reader` from migration 0063 — USAGE on schema
  `audit` only, SELECT on `audit.recency_observation` +
  `audit.coverage_snapshot` only). Even a SQL-injection escape
  cannot reach `streams.outbox.payload`, `doc.filing_body`,
  `audit.events`, or any other internal surface; the role lacks
  USAGE on every other schema.
* DNS: `status.aslan.<domain>` → `:8081` on the container host.
* CDN: optional. The `/status` route already sets
  `Cache-Control: public, max-age=60` so a fronting CDN absorbs
  ~all traffic between cron writes (which run every 5 min).
* Monitoring: external uptime check (UptimeRobot / similar) at a
  30-second interval against `https://status.aslan.<domain>/status`
  — the page is the dogfood signal for the rest of the system.

### Migration 0063

Creates the `public_status_reader` role with:

* LOGIN with its own password (`ASLAN_PUBLIC_STATUS_PASSWORD` env
  var; dev fallback `DEV_ONLY_REPLACE_ME` when `ASLAN_ENV=dev`).
* `default_transaction_read_only=on` as a soft floor.
* USAGE on schema `audit` only.
* SELECT on `audit.recency_observation` +
  `audit.coverage_snapshot` only.

Defense-in-depth posture: separated from the `aslan_dashboard`
role so a misconfigured public-status process logs in to a role
that simply cannot reach internal surfaces — not "is told not to
look" via in-process VMs but is denied at the privilege gate by
PostgreSQL.

### What the page surfaces

* Headline: `Overall: X% Fresh` (unweighted average across
  observed sources; UNKNOWN sources excluded so a never-observed
  source does not drag the headline to 0).
* Per-source rows for KAP / EVDS / BIST / TEFAS / MKK with badge
  (Operational / Degraded / Outage / Unknown), freshness % derived
  from lag vs SLA (linear from 1× SLA = 100% to 2× SLA = 0%, where
  2× is the alert threshold per `audit.recency_sla.alert_at_2x`),
  coverage % from latest `coverage_snapshot.coverage_pct`, and the
  most-recent upstream timestamp rendered as a coarse age label
  ("2 min ago", "3 hr ago", etc.).
* Footer: "Powered by Aslan Terminal" + Terms link.

NO incident history, NO subscribers, NO admin features.
Forward-compatible — the schema reads only from existing tables;
when paying users justify deeper trust commitments (incident
history, postmortems, subscriptions) those features layer on
without DB changes.

### Sources of truth

* Package: `src/aslan_core/public_status/` — sibling to
  `aslan_core.dashboard`. Contains `app.py` (FastHTML app +
  `configure_app`), `pages/status.py` (route + body builder),
  `queries.py` (two `text(...)` literals + pure-function row
  computation), `view_models.py` (frozen pydantic VMs), and
  `render.py` (minimal HTML scaffolding + `Cache-Control` header).
* CLI: `src/aslan_core/cli/public_status.py` — `public-status
  serve` subcommand wired into `aslan_core.cli.main`.
* Migration: `0063_dq_public_status_role.py`.
* Tests:
  * `tests/integration/test_migration_0063_public_status_role.py`
    (18 tests — role + GRANT boundary, deny-list across audit.*
    and non-audit schemas, INSERT denial)
  * `tests/integration/test_public_status_route.py` (7 tests —
    no-auth invariant, no-auth-import canary, Cache-Control header,
    page structure, empty-data UNKNOWN path, fresh-KAP OK path)
  * `tests/unit/test_public_status_queries.py` (24 tests — pure-
    function coverage of `_humanize_age`, `_freshness_pct`,
    `_classify_badge`, `_build_row`, `_build_status`)
