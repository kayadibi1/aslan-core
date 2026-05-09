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
