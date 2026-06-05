# aslan-core

The data-plane core of **Aslan Terminal**, a bitemporal financial-data platform for the Turkish market (BIST equities and KAP disclosures). This package owns the schemas, the database engine, the ingestion plumbing, entity resolution, the LLM-extraction provenance layer, the audit subsystem, and the read-only operator dashboard that the ingestion services build on.

## What is interesting here

The hard correctness properties are enforced in code and in the database, not by convention.

- **Bitemporal, point-in-time correct.** Every Class-A fact table is keyed `(series_id, ts, as_of)`, and `BEFORE UPDATE` reject-triggers (Alembic migration 0044) make history immutable at the database level. Readers do correct point-in-time collapse (latest `as_of <= pit`), so a query can ask "what did we know on date X" and get the right answer.
- **Content-addressed raw-byte provenance.** Every fetched payload is written to object storage before it is parsed, content-addressed by sha256, with a per-fetch provenance sidecar and a header allowlist that scrubs cookies and authorization. Every downstream field is reconstructable from the raw bytes that produced it.
- **Provenance-traced LLM extraction.** Structured extractions carry `model_version`, `prompt_version`, `prompt_sha256`, `input_sha256`, `output_sha256`, token counts, cost, and a primary/verifier/final confidence chain, recorded in an audit hypertable. Extraction is batch-only with budget caps and confidence-gated quarantine.
- **Transactional outbox and a trust-boundary entity registry.** A textbook outbox (outbox and audit rows written atomically inside the caller transaction, with intra-batch dedup) feeds Redis streams. The entity registry enforces a trust boundary so one source cannot hijack another source's entity through identifier attachment.
- **Auditability throughout.** A dedicated data-quality and audit subsystem, plus a read-only operator dashboard over the outbox, streams, deadletter, ingestion, audit, and redaction tables.

Async SQLAlchemy 2.0 throughout, `mypy --strict`, disciplined Alembic migrations. aslan-core alone is roughly 29,000 lines of Python with on the order of 1,000 test functions.

## Honest status

- This is the **core library of a larger, multi-repo system.** The ingestion pullers (KAP, BIST, EVDS, MKK, TEFAS) and some deployment assets live in separate repositories that are not all public, so this repo is not a runnable end-to-end product on its own.
- **Production row counts are operational, not in this repo.** The live deployment holds millions of observations and hundreds of thousands of disclosures, but those figures describe a running database on a private host. Nothing in this tree asserts them.
- A few subsystems are explicit phase-skeletons (the bitemporal research API, some data-quality probes). They are labeled as such where they are not finished, rather than presented as complete.

---

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

The canonical local stack ports: Postgres + TimescaleDB (5432), PgBouncer (6432, `aslan-core` connects here), Redis (6379), MinIO API and console (9000 and 9001). If you also run other Aslan-Terminal scrapers' local stacks, bring them down first; consumer scrapers `extends:` this Compose rather than ship their own database services.

## Dashboard (v0.6.0)

Read-only operator UI for the data plane (outbox, streams, deadletter, ingestion, audit, redactions). Install with the `[dashboard]` extra:

```
uv pip install -e '.[dashboard]'
alembic upgrade head
export ASLAN_DASHBOARD_DSN=postgresql://aslan_dashboard:<password>@localhost/aslan
aslan dashboard serve
```

## License

MIT (see `LICENSE`).
