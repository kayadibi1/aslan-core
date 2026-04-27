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
