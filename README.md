# aslan-core

Shared Python package for Aslan Terminal data-plane services.

- Spec: see `docs/aslan-core-spec.md` in the `crawl` repo (peer document
  to `DATABASE_ARCHITECTURE.md`).
- v0.1 scope: engine/session, Alembic-owned `ref + src + empty agg`
  schemas, `EntityRegistryClient`, `ingestion_run()`, `WatermarkStore`,
  errors, entity/identifier Pydantic models, `aslan migrate / seed /
  registry` CLI.

## Dev install (consumer)

```
uv pip install -e ../aslan-core
```

## Local stack

```
docker compose -f infra/docker-compose.yml up -d
aslan migrate up
aslan seed sources --file infra/sources.yaml
```

## Local stack notes

The canonical aslan local stack uses these ports:

| Port | Service |
|------|---------|
| 5432 | Postgres + TimescaleDB |
| 6432 | PgBouncer (transaction pooling — `aslan-core` connects here) |
| 6379 | Redis |
| 9000 | MinIO API |
| 9001 | MinIO console |

If you also run other Aslan-Terminal scrapers' local stacks (e.g. an older `kap-scraper` Compose that bundles its own Postgres on 5432), bring them down before starting the aslan stack. Per spec §11, consumer scrapers should `extends:` this Compose rather than ship their own DB services — that work happens repo-by-repo as each consumer migrates.

## Dashboard (v0.6.0)

Read-only operator UI for the data plane — outbox, streams,
deadletter, ingestion, audit, redactions. Install with the
`[dashboard]` extra:

```
uv pip install -e '.[dashboard]'
alembic upgrade head
export ASLAN_DASHBOARD_DSN=postgresql://aslan_dashboard:<password>@localhost/aslan
aslan dashboard serve
```

Full runbook + reverse-proxy deployment guide: [`docs/dashboard.md`](docs/dashboard.md).
