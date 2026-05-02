# aslan-evds-puller — Design Spec

**Date:** 2026-05-02
**Status:** Approved
**Repo:** `kayadibi1/aslan-evds-puller` (standalone, single dep on aslan-core)

## Context

EVDS (TCMB Electronic Data Delivery System) is Turkey's central bank macro data API. This is the first timeseries data source for Aslan Terminal, activating the `agg.observation_daily_to_monthly` continuous aggregate and unblocking TAS 29 hyperinflation restatement (which references `evds.macro.cpi.headline`).

### What EVDS provides

- Macro indicators: CPI, PPI, interest rates, money supply, FX rates, reserves, balance of payments
- Daily/monthly/quarterly frequency
- REST API at `https://evds2.tcmb.gov.tr/service/evds` with free API key
- JSON responses with series codes like `TP.FG.J0`, `TP.YSSK.A1`
- Rate limit: 10 req/sec

### Design decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Repo placement | Standalone `aslan-evds-puller` | DBA Appendix A: each source gets own repo, CI, deploy, version |
| Series scope | Full v1 (6 groups, ~50 series) | CPI, FX, policy rates, reserves, monetary, BoP |
| Deployment model | Cron job (daily + monthly) | Mirrors crawl's entity/disclosure sync pattern |
| Local DB state | `evds` schema with series_definition table | Data-driven catalog, enable/disable without redeploy |
| HTTP client | httpx (async) | aslan-core consumer surface is async; crawl proved the pattern |
| Architecture | Monolithic CLI | ~50 series at 10 req/sec = ~5s; no need for parallelism |

## Repo structure

```
aslan-evds-puller/
├── src/evds/
│   ├── __init__.py
│   ├── client.py          # EVDS REST API client (httpx, async, rate-limited)
│   ├── catalog.py         # series manifest + catalog sync logic
│   ├── pull.py            # pull engine (daily + backfill modes)
│   ├── config.py          # pydantic-settings: EVDS_API_KEY, PG DSN, etc.
│   ├── db.py              # local engine/session factory
│   └── cli/
│       ├── __init__.py
│       └── pull.py        # click CLI: evds-pull --mode daily|backfill|seed-catalog
├── migrations/
│   └── versions/
│       └── 20260502_1300_initial_series_definition.py
├── tests/
│   ├── conftest.py
│   ├── fixtures/          # recorded EVDS API responses (JSON)
│   ├── test_client.py
│   ├── test_catalog.py
│   ├── test_pull.py
│   └── test_cli.py
├── pyproject.toml
├── alembic.ini
├── .github/workflows/ci.yml
└── CLAUDE.md
```

## Dependencies

```toml
[project]
name = "aslan-evds-puller"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
    "aslan-core>=0.7.0,<0.8",
    "click>=8.1",
    "httpx>=0.27",
    "asyncpg>=0.29",
    "alembic>=1.13",
    "sqlalchemy[asyncio]>=2.0",
    "structlog>=24.4",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
]

[tool.uv.sources]
aslan-core = { git = "https://github.com/kayadibi1/aslan-core.git", tag = "v0.7.0" }
```

No Redis, no MinIO, no FastAPI — much leaner than crawl. Same GitHub App auth pattern for CI.

## Data model — `evds` schema

One local table for the series catalog:

```sql
CREATE SCHEMA IF NOT EXISTS evds;

CREATE TABLE evds.series_definition (
    series_def_id       SERIAL PRIMARY KEY,
    series_code         TEXT NOT NULL UNIQUE,       -- canonical: 'evds.macro.cpi.headline'
    evds_native_code    TEXT NOT NULL UNIQUE,       -- TCMB: 'TP.FG.J0'
    category            TEXT NOT NULL,              -- 'policy_rate', 'fx', 'cpi', 'reserves', 'monetary', 'bop'
    frequency           TEXT NOT NULL,              -- '1d', '1mo', '1q' (aslan-core Frequency literal)
    unit                TEXT NOT NULL,              -- 'percent', 'TRY', 'USD', 'index', 'million_usd'
    currency_code       TEXT,                       -- 'TRY', 'USD', NULL for dimensionless
    description         TEXT,                       -- 'TCMB 1-Week Repo Rate'
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Interaction with aslan-core

- `seed-catalog` reads enabled rows from `evds.series_definition`, calls `ObservationWriter.upsert_series()` for each, creating/updating `ts.series_catalog` with `source_id='evds'` and `metadata={'evds_native_code': '...'}`.
- `daily` mode queries `evds.series_definition WHERE enabled = TRUE` to get the pull list, uses `series_code` to look up `series_id` from `ts.series_catalog` for observation writes.

### Watermark convention

Uses aslan-core's `src.watermark` (no local table):

- `source_id='evds'`, `job_name='daily_pull'`, `key=<series_code>` → cursor is ISO date of last observation pulled.

### Series manifest

A Python dict in `catalog.py` defines the ~50 series for the initial seed. After `seed-catalog` loads them into `evds.series_definition`, the table becomes the source of truth. Series can be added/enabled/disabled via SQL without redeployment.

**v1 categories:**

| Category | Example series_code | Example native code | Frequency | Unit |
|----------|-------------------|-------------------|-----------|------|
| policy_rate | `evds.macro.policy_rate.tcmb_1week_repo` | `TP.PY.P01` | 1d | percent |
| fx | `evds.fx.usdtry.cb_buying` | `TP.DK.USD.A` | 1d | TRY |
| cpi | `evds.macro.cpi.headline` | `TP.FG.J0` | 1mo | index |
| reserves | `evds.macro.fx_reserves.gross` | `TP.AB.A01` | 1w | million_usd |
| monetary | `evds.macro.monetary.m2` | `TP.PR.M2YP` | 1mo | million_try |
| bop | `evds.macro.bop.current_account` | `TP.OD.Q001` | 1mo | million_usd |

## EVDS API client

`client.py` — thin async wrapper with rate limiting.

```python
class EvdsClient:
    def __init__(self, api_key: str, http: httpx.AsyncClient) -> None: ...

    async def fetch_series(
        self,
        native_code: str,
        start_date: date,
        end_date: date,
    ) -> list[EvdsObservation]:
        """Single series pull. Handles EVDS quirks:
        - Date format DD-MM-YYYY (Turkish convention)
        - Values as strings, some with comma decimal separators
        - NULL/missing values as empty string or "ND"
        - Column name = native code with dots → underscores
        """
        ...

@dataclass(frozen=True, slots=True)
class EvdsObservation:
    date: date
    value: float | None   # None for "ND" / missing
    native_code: str
```

**Rate limiting:** Semaphore + sleep limiter at 8 req/sec (80% of the 10 req/sec limit, safety margin). Applied at the client level.

**Base URL:** `https://evds2.tcmb.gov.tr/service/evds`

**Auth:** `key` query parameter on every request.

## Pull engine

`pull.py` — core logic tying client, catalog, and aslan-core together.

### Daily mode

1. Query `evds.series_definition WHERE enabled = TRUE`
2. Open `ingestion_run(engine, source_id='evds', job_name='daily_pull')`
3. For each series:
   - `WatermarkStore.get('evds', 'daily_pull', series_code)` → last date (or None)
   - If no watermark → start from 30 days ago (not full backfill)
   - `EvdsClient.fetch_series(native_code, start_date, today)`
   - Skip if no new observations
   - `ObservationWriter.upsert_series()` (idempotent — ensures catalog entry exists)
   - `ObservationWriter.write(series_id, observations)`
   - `WatermarkStore.advance('evds', 'daily_pull', series_code, new_cursor, old_cursor)`
   - On failure: log error with structlog, continue to next series
4. Return `PullResult(succeeded=N, failed=M, observations_written=K)`

### Backfill mode

Same loop but ignores watermarks and pulls from `today - 10 years` to `today`. Uses `WatermarkStore.set()` (force) instead of `advance()`.

### Observation mapping

```python
def to_observation_in(obs: EvdsObservation, as_of: date) -> ObservationIn:
    return ObservationIn(
        ts=datetime(obs.date.year, obs.date.month, obs.date.day, tzinfo=UTC),
        as_of=datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC),
        value=obs.value,
        metadata=None,
    )
```

`as_of` = observation date (UTC midnight). EVDS does not provide revision timestamps.

### Error model

Skip-and-continue per series. Failed series logged with structlog (series_code, error, traceback). Next daily run retries from the same watermark.

## CLI

Single Click entrypoint:

```python
@click.command("evds-pull")
@click.option("--mode", type=click.Choice(["daily", "backfill", "seed-catalog"]), required=True)
@click.option("--category", type=str, default=None, help="Filter to one category")
@click.option("--series", type=str, default=None, help="Filter to one series_code")
@click.option("--dry-run", is_flag=True, help="Fetch but don't write to DB")
def cmd(mode, category, series, dry_run): ...
```

**pyproject.toml entrypoint:**
```toml
[project.scripts]
evds-pull = "evds.cli.pull:cmd"
```

## Configuration

```python
class EvdsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EVDS_")

    api_key: str                          # EVDS_API_KEY
    postgres_dsn: str                     # EVDS_POSTGRES_DSN (same DB as aslan-core)
    log_level: str = "INFO"               # EVDS_LOG_LEVEL
    rate_limit_rps: float = 8.0           # EVDS_RATE_LIMIT_RPS
    backfill_years: int = 10              # EVDS_BACKFILL_YEARS
    daily_lookback_days: int = 30         # EVDS_DAILY_LOOKBACK_DAYS (when no watermark)
```

## CI

Mirrors crawl's workflow — same GitHub App, same two secrets:

```yaml
jobs:
  lint:
    # ruff check, ruff format --check, mypy src

  test:
    services:
      postgres: timescale/timescaledb:latest-pg16
    steps:
      # mint GitHub App token → clone aslan-core → run aslan-core migrations
      # → run evds migrations → pytest
```

## Testing strategy

| Test file | What it covers | Infrastructure |
|-----------|---------------|----------------|
| `test_client.py` | EVDS API parsing: comma decimals, "ND" nulls, Turkish dates, empty responses | respx mocks |
| `test_catalog.py` | seed-catalog: `evds.series_definition` + `ts.series_catalog` entries | real Postgres (testcontainers) |
| `test_pull.py` | daily + backfill: observations in `ts.observation`, watermark advance, skip-on-failure | real Postgres + respx |
| `test_cli.py` | Click runner: `--mode`, `--dry-run`, `--category` filtering | Click test runner |

## Deployment on Hetzner

Two cron entries added to existing Docker Compose:

```
# Daily pull: 13:00 UTC (16:00 TRT, after TCMB publishes)
0 13 * * * docker compose run --rm evds-puller evds-pull --mode daily

# Monthly catalog refresh (3rd of each month)
0 6 3 * * docker compose run --rm evds-puller evds-pull --mode seed-catalog
```

**First-time setup:**
```bash
docker compose run --rm evds-puller evds-pull --mode seed-catalog
docker compose run --rm evds-puller evds-pull --mode backfill
```

## What this unblocks

- First timeseries observations → `agg.observation_daily_to_monthly` continuous aggregate activates
- CPI series → TAS 29 restatement job can run (references `evds.macro.cpi.headline`)
- Cross-source queries: KAP filings + EVDS macro data in the same DB
- Stream event `evds.observations.new` (DBA §9.2) — not emitted by the puller itself; a future aslan-core hook on `ObservationWriter.write()` would publish to this stream

## aslan-core APIs consumed

| API | Purpose |
|-----|---------|
| `ObservationWriter.upsert_series()` | Register/update series in `ts.series_catalog` |
| `ObservationWriter.write()` | Bulk write observations to `ts.observation` |
| `WatermarkStore.get()` / `.advance()` / `.set()` | Incremental pull cursors in `src.watermark` |
| `ingestion_run()` | Per-pull bookkeeping in `src.ingestion_run` |
| `Actor` / `current_actor()` | Audit trail identity |
| `Frequency`, `ObservationIn`, `SeriesUpsertResult` | Pydantic schemas |
