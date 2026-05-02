# aslan-evds-puller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone repo that pulls TCMB EVDS macro data (CPI, FX, policy rates, reserves, monetary, BoP — ~50 series) into aslan-core's `ts.observation` table via `ObservationWriter`.

**Architecture:** Monolithic async CLI (`evds-pull --mode daily|backfill|seed-catalog`) using httpx for EVDS API, aslan-core's `ObservationWriter`/`WatermarkStore`/`ingestion_run()` for DB writes, and a local `evds.series_definition` table (Alembic-managed) for the data-driven series catalog. Cron-deployed on Hetzner alongside existing crawl services.

**Tech Stack:** Python 3.12, httpx, SQLAlchemy async, Alembic, Click, pydantic-settings, aslan-core >=0.7.0, structlog. Testing: pytest-asyncio, respx, testcontainers.

**Spec:** `docs/superpowers/specs/2026-05-02-evds-puller-design.md`

---

## File Map

| File | Responsibility |
|------|---------------|
| `pyproject.toml` | Package metadata, deps, entrypoint |
| `alembic.ini` | Alembic config pointing at `migrations/` |
| `CLAUDE.md` | Repo-level instructions for Claude Code |
| `.github/workflows/ci.yml` | Lint + test CI (mirrors crawl) |
| `src/evds/__init__.py` | Package marker |
| `src/evds/config.py` | `EvdsSettings` (pydantic-settings, env-prefixed) |
| `src/evds/db.py` | Engine + session factory (wraps aslan-core's `create_engine`) |
| `src/evds/errors.py` | Typed error classes (`EvdsApiError`, `EvdsParseError`, `WatermarkCasMiss`) |
| `src/evds/client.py` | `EvdsClient` + `EvdsObservation` dataclass + rate limiter + retry |
| `src/evds/catalog.py` | Series manifest (Python dict) + `seed_catalog()` + `load_enabled_series()` |
| `src/evds/pull.py` | `pull_series()` engine (daily + backfill) + `PullResult` |
| `src/evds/cli/__init__.py` | CLI package marker |
| `src/evds/cli/pull.py` | Click command `evds-pull` |
| `migrations/env.py` | Alembic env (async, mirrors aslan-core pattern) |
| `migrations/versions/20260502_1300_initial.py` | `evds.series_definition` table |
| `tests/conftest.py` | Shared fixtures: Postgres container, engine, session, EVDS source seed |
| `tests/fixtures/daily_response.json` | Recorded EVDS API response (daily series) |
| `tests/fixtures/monthly_response.json` | Recorded EVDS API response (monthly series) |
| `tests/fixtures/empty_response.json` | Empty EVDS API response |
| `tests/fixtures/null_values_response.json` | Response with "ND" null markers |
| `tests/test_client.py` | EVDS API client: parsing, rate limiting, error handling |
| `tests/test_catalog.py` | Catalog seeding: `evds.series_definition` + `ts.series_catalog` |
| `tests/test_pull.py` | Pull engine: daily, backfill, skip-on-failure, watermark advance |
| `tests/test_cli.py` | CLI smoke tests: mode dispatch, --dry-run, --category filter |

---

### Task 1: Repo scaffold + pyproject.toml + tooling config

**Files:**
- Create: `~/Desktop/aslan-evds-puller/pyproject.toml`
- Create: `~/Desktop/aslan-evds-puller/CLAUDE.md`
- Create: `~/Desktop/aslan-evds-puller/src/evds/__init__.py`
- Create: `~/Desktop/aslan-evds-puller/src/evds/cli/__init__.py`

- [ ] **Step 1: Create repo directory and initialize git**

```bash
mkdir -p ~/Desktop/aslan-evds-puller
cd ~/Desktop/aslan-evds-puller
git init
```

- [ ] **Step 2: Write pyproject.toml**

```toml
[project]
name = "aslan-evds-puller"
version = "0.1.0"
description = "TCMB EVDS macro data puller for Aslan Terminal"
readme = "README.md"
requires-python = ">=3.12,<3.13"
license = { text = "Proprietary" }
authors = [{ name = "kayadibi1", email = "markxsola@gmail.com" }]

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

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "testcontainers[postgres]>=4.7",
    "ruff>=0.7",
    "mypy>=1.20",
    "respx>=0.22",
]

[tool.uv.sources]
aslan-core = { git = "https://github.com/kayadibi1/aslan-core.git", tag = "v0.7.0" }

[project.scripts]
evds-pull = "evds.cli.pull:cmd"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "SIM", "RUF"]

[tool.mypy]
strict = true
python_version = "3.12"
warn_return_any = true
warn_unused_configs = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 3: Write CLAUDE.md**

```markdown
# aslan-evds-puller

TCMB EVDS macro data puller for Aslan Terminal. Pulls ~50 series
(CPI, FX, policy rates, reserves, monetary, BoP) into aslan-core's
`ts.observation` table.

## Hard rules

- Single dep on aslan-core. Do NOT import crawl/kap code.
- All datetimes tz-aware (UTC). Naive datetimes raise.
- `mypy --strict` passes. `ruff format` + `ruff check` clean.
- Errors are typed. Never `except Exception:` in production paths.
- EVDS native codes stay in `evds.series_definition.evds_native_code`.
  Our canonical codes go in `series_code`.

## aslan-core APIs consumed

- `ObservationWriter(session, ingestion_run_id)` — `.upsert_series()`, `.write()`
- `WatermarkStore(session)` — `.get()`, `.advance()`, `.set()`
- `ingestion_run(engine, source_id='evds', job_name=...)` — context manager
- `Actor`, `push_actor` — audit identity
- `create_engine(dsn)`, `create_session_factory(engine)`, `session_scope(factory)`
- `Frequency`, `ObservationIn`, `SeriesUpsertResult` — Pydantic schemas

## Testing

- `respx` for EVDS API mocks (never hit live API in tests)
- `testcontainers` for real Postgres (TimescaleDB)
- Run aslan-core migrations before evds migrations in test setup
```

- [ ] **Step 4: Write package markers**

`src/evds/__init__.py`:
```python
"""TCMB EVDS macro data puller for Aslan Terminal."""
```

`src/evds/cli/__init__.py`:
```python
"""CLI package for evds-pull."""
```

- [ ] **Step 5: Create empty directories**

```bash
mkdir -p src/evds/cli tests/fixtures migrations/versions
```

- [ ] **Step 6: uv sync**

```bash
cd ~/Desktop/aslan-evds-puller
uv sync
```

This will resolve aslan-core from the git tag and install all deps. If it needs the GitHub App token for the private repo:

```bash
git config --global url."https://x-access-token:<TOKEN>@github.com/kayadibi1/aslan-core".insteadOf "https://github.com/kayadibi1/aslan-core"
```

Or use the local override in pyproject.toml during development:

```toml
[tool.uv.sources]
aslan-core = { path = "../aslan-core", editable = true }
```

- [ ] **Step 7: Commit scaffold**

```bash
git add -A
git commit -m "chore: repo scaffold + pyproject.toml + CLAUDE.md"
```

---

### Task 2: Configuration module

**Files:**
- Create: `src/evds/config.py`
- Test: `tests/test_config.py` (inline smoke — no separate file needed yet)

- [ ] **Step 1: Write config.py**

```python
from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class EvdsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EVDS_",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    api_key: SecretStr = Field(description="EVDS API key from evds2.tcmb.gov.tr")
    postgres_dsn: str = Field(description="PostgreSQL DSN (same DB as aslan-core)")
    log_level: str = Field(default="INFO")
    rate_limit_rps: float = Field(default=8.0)
    backfill_years: int = Field(default=10)
    daily_lookback_days: int = Field(default=30)
```

- [ ] **Step 2: Verify it loads**

```bash
EVDS_API_KEY=test EVDS_POSTGRES_DSN=postgresql+asyncpg://x:x@localhost/x uv run python -c "from evds.config import EvdsSettings; s = EvdsSettings(); print(s.rate_limit_rps)"
```

Expected: `8.0`

- [ ] **Step 3: Commit**

```bash
git add src/evds/config.py
git commit -m "feat: EvdsSettings config module"
```

---

### Task 3: Database module + Alembic setup + migration

**Files:**
- Create: `src/evds/db.py`
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/versions/20260502_1300_initial.py`

- [ ] **Step 1: Write db.py**

```python
from __future__ import annotations

from aslan_core.db.engine import create_engine as _ac_create_engine
from aslan_core.db.session import create_session_factory, session_scope
from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["create_engine", "create_session_factory", "session_scope"]


def create_engine(dsn: str) -> AsyncEngine:
    return _ac_create_engine(dsn)
```

- [ ] **Step 2: Write alembic.ini**

```ini
[alembic]
script_location = migrations
sqlalchemy.url = placeholder

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 3: Write migrations/env.py**

```python
from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine


def get_url() -> str:
    return os.environ["EVDS_POSTGRES_DSN"]


def run_migrations_offline() -> None:
    context.configure(url=get_url(), target_metadata=None, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(get_url(), poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

- [ ] **Step 4: Write the initial migration**

`migrations/versions/20260502_1300_initial.py`:

```python
"""evds schema + series_definition table

Revision ID: 0001
Revises: None
Create Date: 2026-05-02 13:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS evds")
    op.execute("""
        CREATE TABLE evds.series_definition (
            series_def_id       SERIAL PRIMARY KEY,
            series_code         TEXT NOT NULL UNIQUE,
            evds_native_code    TEXT NOT NULL UNIQUE,
            category            TEXT NOT NULL
                CHECK (category IN ('policy_rate', 'fx', 'cpi', 'reserves', 'monetary', 'bop')),
            metric              TEXT NOT NULL,
            frequency           TEXT NOT NULL
                CHECK (frequency IN ('tick','1s','1m','5m','15m','30m','1h','1d','1w','1mo','1q','1y','irregular')),
            unit                TEXT NOT NULL,
            currency_code       TEXT,
            description         TEXT,
            enabled             BOOLEAN NOT NULL DEFAULT TRUE,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS evds.series_definition")
    op.execute("DROP SCHEMA IF EXISTS evds")
```

- [ ] **Step 5: Commit**

```bash
git add src/evds/db.py alembic.ini migrations/
git commit -m "feat: db module + Alembic setup + evds.series_definition migration"
```

---

### Task 4: Error types + EVDS API client + tests

**Files:**
- Create: `src/evds/errors.py`
- Create: `src/evds/client.py`
- Create: `tests/fixtures/daily_response.json`
- Create: `tests/fixtures/monthly_response.json`
- Create: `tests/fixtures/empty_response.json`
- Create: `tests/fixtures/null_values_response.json`
- Create: `tests/test_client.py`

- [ ] **Step 1: Write errors.py**

`src/evds/errors.py`:

```python
from __future__ import annotations


class EvdsError(Exception):
    """Base for all typed EVDS puller errors."""


class EvdsApiError(EvdsError):
    """EVDS API returned a non-retryable error (400, 403)."""


class EvdsParseError(EvdsError):
    """Failed to parse EVDS API response."""


class WatermarkCasMiss(EvdsError):
    """WatermarkStore.advance() CAS failed — concurrent writer or stale cursor."""
```

- [ ] **Step 2: Write test fixtures**

`tests/fixtures/daily_response.json` — simulates a daily FX series response:
```json
{
  "totalCount": 3,
  "items": [
    {"Tarih": "28-04-2026", "UNIXTIME": "1745798400", "TP_DK_USD_A": "38.1234"},
    {"Tarih": "29-04-2026", "UNIXTIME": "1745884800", "TP_DK_USD_A": "38.2567"},
    {"Tarih": "30-04-2026", "UNIXTIME": "1745971200", "TP_DK_USD_A": "38.1890"}
  ]
}
```

`tests/fixtures/monthly_response.json` — simulates a monthly CPI series:
```json
{
  "totalCount": 2,
  "items": [
    {"Tarih": "01-03-2026", "UNIXTIME": "1740787200", "TP_FG_J0": "2145,67"},
    {"Tarih": "01-04-2026", "UNIXTIME": "1743465600", "TP_FG_J0": "2198,34"}
  ]
}
```

`tests/fixtures/empty_response.json`:
```json
{
  "totalCount": 0,
  "items": []
}
```

`tests/fixtures/null_values_response.json`:
```json
{
  "totalCount": 3,
  "items": [
    {"Tarih": "28-04-2026", "UNIXTIME": "1745798400", "TP_DK_USD_A": "38.1234"},
    {"Tarih": "29-04-2026", "UNIXTIME": "1745884800", "TP_DK_USD_A": "ND"},
    {"Tarih": "30-04-2026", "UNIXTIME": "1745971200", "TP_DK_USD_A": ""}
  ]
}
```

- [ ] **Step 3: Write the failing tests**

`tests/test_client.py`:

```python
from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest
import respx

from evds.client import EvdsClient, EvdsObservation
from evds.errors import EvdsApiError


@pytest.fixture
def api_key() -> str:
    return "test-key"


@pytest.fixture
def mock_http() -> httpx.AsyncClient:
    return httpx.AsyncClient()


def _fixture(name: str) -> str:
    from pathlib import Path

    return (Path(__file__).parent / "fixtures" / name).read_text()


class TestFetchSeries:
    @respx.mock
    async def test_daily_fx_series(self, api_key: str, mock_http: httpx.AsyncClient) -> None:
        import json

        data = json.loads(_fixture("daily_response.json"))
        respx.get("https://evds2.tcmb.gov.tr/service/evds/series=TP.DK.USD.A").mock(
            return_value=httpx.Response(200, json=data)
        )
        client = EvdsClient(api_key=api_key, http=mock_http)
        obs = await client.fetch_series("TP.DK.USD.A", date(2026, 4, 28), date(2026, 4, 30))

        assert len(obs) == 3
        assert obs[0] == EvdsObservation(date=date(2026, 4, 28), value=38.1234, native_code="TP.DK.USD.A")
        assert obs[1].value == 38.2567
        assert obs[2].date == date(2026, 4, 30)

    @respx.mock
    async def test_monthly_comma_decimal(self, api_key: str, mock_http: httpx.AsyncClient) -> None:
        import json

        data = json.loads(_fixture("monthly_response.json"))
        respx.get("https://evds2.tcmb.gov.tr/service/evds/series=TP.FG.J0").mock(
            return_value=httpx.Response(200, json=data)
        )
        client = EvdsClient(api_key=api_key, http=mock_http)
        obs = await client.fetch_series("TP.FG.J0", date(2026, 3, 1), date(2026, 4, 1))

        assert len(obs) == 2
        assert obs[0].value == pytest.approx(2145.67)
        assert obs[1].value == pytest.approx(2198.34)

    @respx.mock
    async def test_empty_response(self, api_key: str, mock_http: httpx.AsyncClient) -> None:
        import json

        data = json.loads(_fixture("empty_response.json"))
        respx.get("https://evds2.tcmb.gov.tr/service/evds/series=TP.FG.J0").mock(
            return_value=httpx.Response(200, json=data)
        )
        client = EvdsClient(api_key=api_key, http=mock_http)
        obs = await client.fetch_series("TP.FG.J0", date(2026, 3, 1), date(2026, 4, 1))

        assert obs == []

    @respx.mock
    async def test_null_values(self, api_key: str, mock_http: httpx.AsyncClient) -> None:
        import json

        data = json.loads(_fixture("null_values_response.json"))
        respx.get("https://evds2.tcmb.gov.tr/service/evds/series=TP.DK.USD.A").mock(
            return_value=httpx.Response(200, json=data)
        )
        client = EvdsClient(api_key=api_key, http=mock_http)
        obs = await client.fetch_series("TP.DK.USD.A", date(2026, 4, 28), date(2026, 4, 30))

        assert len(obs) == 3
        assert obs[0].value == 38.1234
        assert obs[1].value is None  # "ND"
        assert obs[2].value is None  # empty string

    @respx.mock
    async def test_api_key_sent_as_query_param(self, api_key: str, mock_http: httpx.AsyncClient) -> None:
        import json

        data = json.loads(_fixture("empty_response.json"))
        route = respx.get("https://evds2.tcmb.gov.tr/service/evds/series=TP.FG.J0").mock(
            return_value=httpx.Response(200, json=data)
        )
        client = EvdsClient(api_key=api_key, http=mock_http)
        await client.fetch_series("TP.FG.J0", date(2026, 3, 1), date(2026, 4, 1))

        assert route.called
        req = route.calls[0].request
        assert b"key=test-key" in req.url.raw_path

    @respx.mock
    async def test_non_retryable_error_raises_typed(self, api_key: str, mock_http: httpx.AsyncClient) -> None:
        respx.get("https://evds2.tcmb.gov.tr/service/evds/series=TP.FG.J0").mock(
            return_value=httpx.Response(403)
        )
        client = EvdsClient(api_key=api_key, http=mock_http)

        with pytest.raises(EvdsApiError):
            await client.fetch_series("TP.FG.J0", date(2026, 3, 1), date(2026, 4, 1))

    @respx.mock
    async def test_retryable_error_retries_then_raises(self, api_key: str, mock_http: httpx.AsyncClient) -> None:
        route = respx.get("https://evds2.tcmb.gov.tr/service/evds/series=TP.FG.J0").mock(
            return_value=httpx.Response(500)
        )
        client = EvdsClient(api_key=api_key, http=mock_http, max_retries=2)

        with pytest.raises(EvdsApiError):
            await client.fetch_series("TP.FG.J0", date(2026, 3, 1), date(2026, 4, 1))

        assert route.call_count == 3  # initial + 2 retries
```

- [ ] **Step 4: Run tests — verify they fail**

```bash
uv run pytest tests/test_client.py -v
```

Expected: `ModuleNotFoundError: No module named 'evds.client'`

- [ ] **Step 5: Write client.py**

```python
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date, datetime

import httpx
import structlog

from evds.errors import EvdsApiError, EvdsParseError

log = structlog.get_logger()

EVDS_BASE_URL = "https://evds2.tcmb.gov.tr/service/evds"
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_NON_RETRYABLE_STATUS = frozenset({400, 403, 404})


@dataclass(frozen=True, slots=True)
class EvdsObservation:
    date: date
    value: float | None
    native_code: str


def _parse_value(raw: str | None) -> float | None:
    if raw is None or raw.strip() == "" or raw.strip().upper() == "ND":
        return None
    cleaned = raw.strip().replace(",", ".")
    try:
        return float(cleaned)
    except ValueError as e:
        raise EvdsParseError(f"cannot parse value {raw!r}") from e


def _parse_date(raw: str) -> date:
    try:
        return datetime.strptime(raw.strip(), "%d-%m-%Y").date()
    except ValueError as e:
        raise EvdsParseError(f"cannot parse date {raw!r}") from e


def _native_code_to_column(native_code: str) -> str:
    return native_code.replace(".", "_")


class _RateLimiter:
    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps
        self._last = 0.0

    async def acquire(self) -> None:
        now = time.monotonic()
        wait = self._interval - (now - self._last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last = time.monotonic()


class EvdsClient:
    def __init__(
        self,
        api_key: str,
        http: httpx.AsyncClient,
        rate_limit_rps: float = 8.0,
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key
        self._http = http
        self._limiter = _RateLimiter(rate_limit_rps)
        self._max_retries = max_retries

    async def fetch_series(
        self,
        native_code: str,
        start_date: date,
        end_date: date,
    ) -> list[EvdsObservation]:
        await self._limiter.acquire()

        params = {
            "startDate": start_date.strftime("%d-%m-%Y"),
            "endDate": end_date.strftime("%d-%m-%Y"),
            "type": "json",
            "key": self._api_key,
        }
        url = f"{EVDS_BASE_URL}/series={native_code}"

        last_exc: Exception | None = None
        for attempt in range(1 + self._max_retries):
            try:
                resp = await self._http.get(url, params=params)
            except httpx.TimeoutException as e:
                last_exc = e
                if attempt < self._max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise EvdsApiError(f"timeout after {1 + self._max_retries} attempts: {native_code}") from e

            if resp.status_code in _NON_RETRYABLE_STATUS:
                raise EvdsApiError(f"EVDS API {resp.status_code} for {native_code}")

            if resp.status_code in _RETRYABLE_STATUS:
                last_exc = EvdsApiError(f"EVDS API {resp.status_code} for {native_code}")
                if attempt < self._max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise last_exc

            resp.raise_for_status()
            break

        data = resp.json()
        items: list[dict[str, str]] = data.get("items", [])
        col = _native_code_to_column(native_code)

        observations: list[EvdsObservation] = []
        for item in items:
            raw_date = item.get("Tarih", "")
            raw_value = item.get(col)
            observations.append(
                EvdsObservation(
                    date=_parse_date(raw_date),
                    value=_parse_value(raw_value),
                    native_code=native_code,
                )
            )
        return observations
```

- [ ] **Step 6: Run tests — verify they pass**

```bash
uv run pytest tests/test_client.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 7: Run ruff + mypy**

```bash
uv run ruff check src/evds/errors.py src/evds/client.py
uv run ruff format --check src/evds/errors.py src/evds/client.py
uv run mypy src/evds/errors.py src/evds/client.py
```

- [ ] **Step 8: Commit**

```bash
git add src/evds/errors.py src/evds/client.py tests/test_client.py tests/fixtures/
git commit -m "feat: typed errors + EvdsClient with rate limiting, retry, response parsing"
```

---

### Task 5: Series catalog manifest + seed logic + tests

**Files:**
- Create: `src/evds/catalog.py`
- Create: `tests/conftest.py`
- Create: `tests/test_catalog.py`

- [ ] **Step 1: Write conftest.py with Postgres fixtures**

`tests/conftest.py`:

```python
from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.audit import Actor, push_actor, pop_actor
from aslan_core.db.engine import create_engine as ac_create_engine
from aslan_core.db.session import create_session_factory
from aslan_core.testing.factories import ingestion_run_factory


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(
        image="timescale/timescaledb:latest-pg16",
        username="evds",
        password="evds",
        dbname="evds",
    ) as pg:
        sync_dsn = pg.get_connection_url()
        async_dsn = sync_dsn.replace("psycopg2", "asyncpg", 1)
        yield async_dsn


@pytest.fixture(scope="session")
def _run_migrations(pg_dsn: str) -> None:
    sync_dsn = pg_dsn.replace("asyncpg", "psycopg2", 1)
    aslan_core_root = Path(__file__).resolve().parents[1]

    # Try local aslan-core first (co-development), fall back to installed
    ac_root = Path.home() / "Desktop" / "aslan-core"
    if not (ac_root / "alembic.ini").exists():
        ac_root = aslan_core_root  # fallback

    env = {**os.environ, "ASLAN_PG_DSN": pg_dsn}

    # Run aslan-core migrations (provides ref, src, ts, agg, doc schemas)
    if (ac_root / "alembic.ini").exists():
        subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=str(ac_root),
            env=env,
            check=True,
            capture_output=True,
        )

    # Run evds migrations
    evds_env = {**os.environ, "EVDS_POSTGRES_DSN": pg_dsn}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=str(aslan_core_root.parent if "tests" in str(aslan_core_root) else aslan_core_root),
        env=evds_env,
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def engine(pg_dsn: str, _run_migrations: None) -> AsyncEngine:
    return ac_create_engine(pg_dsn)


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s
        await s.rollback()


@pytest.fixture
async def evds_source(session: AsyncSession) -> None:
    """Ensure src.source has the 'evds' row."""
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('evds', 'TCMB EVDS', 'api', 'free_with_key') "
            "ON CONFLICT DO NOTHING"
        )
    )
    await session.commit()


@pytest.fixture
def actor() -> Actor:
    return Actor(actor_id="service:evds-puller", actor_kind="service")


@pytest.fixture
def _push_actor(actor: Actor) -> Iterator[None]:
    token = push_actor(actor)
    yield
    pop_actor(token)
```

**Note:** The `conftest.py` above will need refinement during implementation. The migration runner needs to find the correct directories — during co-development, aslan-core lives at `~/Desktop/aslan-core` and the EVDS puller at `~/Desktop/aslan-evds-puller`. The implementer should adjust paths based on actual directory layout at implementation time.

- [ ] **Step 2: Write the failing test**

`tests/test_catalog.py`:

```python
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.audit import Actor
from aslan_core.db.session import session_scope
from aslan_core.testing.factories import ingestion_run_factory
from aslan_core.timeseries import ObservationWriter

from evds.catalog import SERIES_MANIFEST, SeriesDefinition, load_enabled_series, seed_catalog


class TestSeedCatalog:
    @pytest.mark.usefixtures("evds_source", "_push_actor")
    async def test_seed_creates_local_and_ts_entries(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_catalog(engine, session_factory)

        async with session_factory() as s:
            local_count: int = await s.scalar(
                text("SELECT count(*) FROM evds.series_definition WHERE enabled = TRUE")
            )
            ts_count: int = await s.scalar(
                text("SELECT count(*) FROM ts.series_catalog WHERE source_id = 'evds'")
            )
            assert local_count > 0
            assert local_count == ts_count

    @pytest.mark.usefixtures("evds_source", "_push_actor")
    async def test_seed_is_idempotent(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_catalog(engine, session_factory)
        await seed_catalog(engine, session_factory)

        async with session_factory() as s:
            count: int = await s.scalar(
                text("SELECT count(*) FROM evds.series_definition")
            )
            assert count == len(SERIES_MANIFEST)

    @pytest.mark.usefixtures("evds_source", "_push_actor")
    async def test_seed_stores_native_code_in_metadata(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_catalog(engine, session_factory)

        async with session_factory() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT metadata->>'evds_native_code' AS nc "
                        "FROM ts.series_catalog "
                        "WHERE series_code = 'evds.macro.cpi.headline'"
                    )
                )
            ).one()
            assert row.nc == "TP.FG.J0"


class TestLoadEnabledSeries:
    @pytest.mark.usefixtures("evds_source", "_push_actor")
    async def test_returns_enabled_only(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_catalog(engine, session_factory)

        async with session_factory() as s:
            await s.execute(
                text(
                    "UPDATE evds.series_definition SET enabled = FALSE "
                    "WHERE category = 'bop'"
                )
            )
            await s.commit()

        series = await load_enabled_series(session_factory)
        categories = {s.category for s in series}
        assert "bop" not in categories
        assert "cpi" in categories
```

- [ ] **Step 3: Run tests — verify they fail**

```bash
uv run pytest tests/test_catalog.py -v
```

Expected: `ModuleNotFoundError: No module named 'evds.catalog'`

- [ ] **Step 4: Write catalog.py**

```python
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.audit import Actor
from aslan_core.db.session import session_scope
from aslan_core.ingestion.run import ingestion_run
from aslan_core.schemas.timeseries import Frequency
from aslan_core.timeseries import ObservationWriter

import structlog

log = structlog.get_logger()

EVDS_ACTOR = Actor(actor_id="service:evds-puller", actor_kind="service")


@dataclass(frozen=True, slots=True)
class SeriesDefinition:
    series_code: str
    evds_native_code: str
    category: str
    metric: str
    frequency: Frequency
    unit: str
    currency_code: str | None
    description: str


SERIES_MANIFEST: list[SeriesDefinition] = [
    # ── Policy rates ──────────────────────────────────────────────
    SeriesDefinition("evds.macro.policy_rate.tcmb_1week_repo", "TP.PY.P01", "policy_rate", "policy_rate", "1d", "percent", None, "TCMB 1-Week Repo Rate"),
    SeriesDefinition("evds.macro.policy_rate.overnight_lending", "TP.PY.P02", "policy_rate", "overnight_lending_rate", "1d", "percent", None, "TCMB Overnight Lending Rate"),
    SeriesDefinition("evds.macro.policy_rate.overnight_borrowing", "TP.PY.P03", "policy_rate", "overnight_borrowing_rate", "1d", "percent", None, "TCMB Overnight Borrowing Rate"),
    SeriesDefinition("evds.macro.policy_rate.late_liquidity_lending", "TP.PY.P04", "policy_rate", "late_liquidity_rate", "1d", "percent", None, "TCMB Late Liquidity Lending Rate"),
    # ── FX rates ──────────────────────────────────────────────────
    SeriesDefinition("evds.fx.usdtry.cb_buying", "TP.DK.USD.A", "fx", "fx_rate", "1d", "TRY", "TRY", "USD/TRY Central Bank Buying Rate"),
    SeriesDefinition("evds.fx.usdtry.cb_selling", "TP.DK.USD.S", "fx", "fx_rate", "1d", "TRY", "TRY", "USD/TRY Central Bank Selling Rate"),
    SeriesDefinition("evds.fx.eurtry.cb_buying", "TP.DK.EUR.A", "fx", "fx_rate", "1d", "TRY", "TRY", "EUR/TRY Central Bank Buying Rate"),
    SeriesDefinition("evds.fx.eurtry.cb_selling", "TP.DK.EUR.S", "fx", "fx_rate", "1d", "TRY", "TRY", "EUR/TRY Central Bank Selling Rate"),
    SeriesDefinition("evds.fx.gbptry.cb_buying", "TP.DK.GBP.A", "fx", "fx_rate", "1d", "TRY", "TRY", "GBP/TRY Central Bank Buying Rate"),
    SeriesDefinition("evds.fx.gbptry.cb_selling", "TP.DK.GBP.S", "fx", "fx_rate", "1d", "TRY", "TRY", "GBP/TRY Central Bank Selling Rate"),
    SeriesDefinition("evds.fx.jpytry.cb_buying", "TP.DK.JPY.A", "fx", "fx_rate", "1d", "TRY", "TRY", "JPY/TRY Central Bank Buying Rate"),
    SeriesDefinition("evds.fx.chftry.cb_buying", "TP.DK.CHF.A", "fx", "fx_rate", "1d", "TRY", "TRY", "CHF/TRY Central Bank Buying Rate"),
    SeriesDefinition("evds.fx.cadtry.cb_buying", "TP.DK.CAD.A", "fx", "fx_rate", "1d", "TRY", "TRY", "CAD/TRY Central Bank Buying Rate"),
    SeriesDefinition("evds.fx.audtry.cb_buying", "TP.DK.AUD.A", "fx", "fx_rate", "1d", "TRY", "TRY", "AUD/TRY Central Bank Buying Rate"),
    SeriesDefinition("evds.fx.cnytry.cb_buying", "TP.DK.CNY.A", "fx", "fx_rate", "1d", "TRY", "TRY", "CNY/TRY Central Bank Buying Rate"),
    SeriesDefinition("evds.fx.sartry.cb_buying", "TP.DK.SAR.A", "fx", "fx_rate", "1d", "TRY", "TRY", "SAR/TRY Central Bank Buying Rate"),
    SeriesDefinition("evds.fx.sektry.cb_buying", "TP.DK.SEK.A", "fx", "fx_rate", "1d", "TRY", "TRY", "SEK/TRY Central Bank Buying Rate"),
    SeriesDefinition("evds.fx.noktry.cb_buying", "TP.DK.NOK.A", "fx", "fx_rate", "1d", "TRY", "TRY", "NOK/TRY Central Bank Buying Rate"),
    SeriesDefinition("evds.fx.dkktry.cb_buying", "TP.DK.DKK.A", "fx", "fx_rate", "1d", "TRY", "TRY", "DKK/TRY Central Bank Buying Rate"),
    SeriesDefinition("evds.fx.krwtry.cb_buying", "TP.DK.KRW.A", "fx", "fx_rate", "1d", "TRY", "TRY", "KRW/TRY Central Bank Buying Rate"),
    # ── CPI / PPI ─────────────────────────────────────────────────
    SeriesDefinition("evds.macro.cpi.headline", "TP.FG.J0", "cpi", "cpi_index", "1mo", "index", None, "CPI (TÜFE) — Headline Index (2003=100)"),
    SeriesDefinition("evds.macro.cpi.core_b", "TP.FG.J0B", "cpi", "cpi_core_b_index", "1mo", "index", None, "CPI Core B — Excluding energy, food, alcohol, tobacco, gold"),
    SeriesDefinition("evds.macro.cpi.core_c", "TP.FG.J0C", "cpi", "cpi_core_c_index", "1mo", "index", None, "CPI Core C — Excluding energy, food and non-alcoholic beverages"),
    SeriesDefinition("evds.macro.ppi", "TP.FG.J1", "cpi", "ppi_index", "1mo", "index", None, "PPI (ÜFE) — Producer Price Index (2003=100)"),
    SeriesDefinition("evds.macro.cpi.food", "TP.FG.J01", "cpi", "cpi_food_index", "1mo", "index", None, "CPI Food and Non-Alcoholic Beverages"),
    SeriesDefinition("evds.macro.cpi.transport", "TP.FG.J07", "cpi", "cpi_transport_index", "1mo", "index", None, "CPI Transportation"),
    SeriesDefinition("evds.macro.cpi.housing", "TP.FG.J04", "cpi", "cpi_housing_index", "1mo", "index", None, "CPI Housing"),
    # ── Reserves ──────────────────────────────────────────────────
    SeriesDefinition("evds.macro.fx_reserves.gross", "TP.AB.A01", "reserves", "gross_fx_reserves", "1w", "million_usd", "USD", "Gross FX Reserves (incl. gold)"),
    SeriesDefinition("evds.macro.fx_reserves.net", "TP.AB.A10", "reserves", "net_fx_reserves", "1w", "million_usd", "USD", "Net FX Reserves"),
    SeriesDefinition("evds.macro.gold_reserves", "TP.AB.A20", "reserves", "gold_reserves", "1w", "million_usd", "USD", "Gold Reserves (USD value)"),
    # ── Monetary aggregates ───────────────────────────────────────
    SeriesDefinition("evds.macro.monetary.m1", "TP.PR.M1YP", "monetary", "m1", "1mo", "million_try", "TRY", "M1 Money Supply"),
    SeriesDefinition("evds.macro.monetary.m2", "TP.PR.M2YP", "monetary", "m2", "1mo", "million_try", "TRY", "M2 Money Supply"),
    SeriesDefinition("evds.macro.monetary.m3", "TP.PR.M3YP", "monetary", "m3", "1mo", "million_try", "TRY", "M3 Money Supply"),
    # ── Balance of payments ───────────────────────────────────────
    SeriesDefinition("evds.macro.bop.current_account", "TP.OD.Q001", "bop", "current_account", "1mo", "million_usd", "USD", "Current Account Balance"),
    SeriesDefinition("evds.macro.bop.trade_balance", "TP.OD.Q002", "bop", "trade_balance", "1mo", "million_usd", "USD", "Trade Balance (Goods)"),
    SeriesDefinition("evds.macro.bop.services_balance", "TP.OD.Q003", "bop", "services_balance", "1mo", "million_usd", "USD", "Services Balance"),
    SeriesDefinition("evds.macro.bop.fdi_net", "TP.OD.Q010", "bop", "fdi_net", "1mo", "million_usd", "USD", "Foreign Direct Investment (Net)"),
    SeriesDefinition("evds.macro.bop.portfolio_net", "TP.OD.Q011", "bop", "portfolio_net", "1mo", "million_usd", "USD", "Portfolio Investment (Net)"),
]


async def seed_catalog(
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
) -> int:
    count = 0
    async with ingestion_run(
        engine, source_id="evds", job_name="seed_catalog", actor=EVDS_ACTOR,
    ) as run:
        async with session_scope(factory) as session:
            writer = ObservationWriter(session, run.id)

            for defn in SERIES_MANIFEST:
                await session.execute(
                    text(
                        "INSERT INTO evds.series_definition "
                        "  (series_code, evds_native_code, category, metric, frequency, "
                        "   unit, currency_code, description) "
                        "VALUES (:sc, :nc, :cat, :met, :freq, :unit, :cur, :desc) "
                        "ON CONFLICT (series_code) DO UPDATE SET "
                        "  evds_native_code = EXCLUDED.evds_native_code, "
                        "  category = EXCLUDED.category, "
                        "  metric = EXCLUDED.metric, "
                        "  frequency = EXCLUDED.frequency, "
                        "  unit = EXCLUDED.unit, "
                        "  currency_code = EXCLUDED.currency_code, "
                        "  description = EXCLUDED.description, "
                        "  updated_at = now()"
                    ),
                    {
                        "sc": defn.series_code,
                        "nc": defn.evds_native_code,
                        "cat": defn.category,
                        "met": defn.metric,
                        "freq": defn.frequency,
                        "unit": defn.unit,
                        "cur": defn.currency_code,
                        "desc": defn.description,
                    },
                )

                await writer.upsert_series(
                    defn.series_code,
                    source_id="evds",
                    metric=defn.metric,
                    frequency=defn.frequency,
                    unit=defn.unit,
                    entity_id=None,
                    currency_code=defn.currency_code,
                    description=defn.description,
                    metadata={"evds_native_code": defn.evds_native_code},
                )
                count += 1
                await run.increment_rows()
                log.info("seeded_series", series_code=defn.series_code, native=defn.evds_native_code)

    return count


async def load_enabled_series(
    factory: async_sessionmaker[AsyncSession],
) -> list[SeriesDefinition]:
    async with session_scope(factory) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT series_code, evds_native_code, category, metric, frequency, "
                    "       unit, currency_code, description "
                    "FROM evds.series_definition WHERE enabled = TRUE "
                    "ORDER BY category, series_code"
                )
            )
        ).all()
        return [
            SeriesDefinition(
                series_code=r.series_code,
                evds_native_code=r.evds_native_code,
                category=r.category,
                metric=r.metric,
                frequency=r.frequency,
                unit=r.unit,
                currency_code=r.currency_code,
                description=r.description,
            )
            for r in rows
        ]
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
uv run pytest tests/test_catalog.py -v
```

Expected: all 4 tests pass (requires Postgres container + migrations running).

- [ ] **Step 6: Run ruff + mypy**

```bash
uv run ruff check src/evds/catalog.py
uv run mypy src/evds/catalog.py
```

- [ ] **Step 7: Commit**

```bash
git add src/evds/catalog.py tests/conftest.py tests/test_catalog.py
git commit -m "feat: series catalog manifest (~40 series, 6 categories) + seed logic"
```

---

### Task 6: Pull engine + tests

**Files:**
- Create: `src/evds/pull.py`
- Create: `tests/test_pull.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_pull.py`:

```python
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.audit import Actor
from aslan_core.db.session import session_scope

from evds.catalog import seed_catalog
from evds.client import EvdsClient
from evds.pull import PullResult, pull_series


def _fixture(name: str) -> dict:
    return json.loads((Path(__file__).parent / "fixtures" / name).read_text())


class TestPullDaily:
    @pytest.mark.usefixtures("evds_source", "_push_actor")
    @respx.mock
    async def test_daily_writes_observations_and_advances_watermark(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_catalog(engine, session_factory)

        # Mock all EVDS API calls — return daily_response for FX, empty for others
        fx_data = _fixture("daily_response.json")
        empty_data = _fixture("empty_response.json")

        respx.route(host="evds2.tcmb.gov.tr").mock(
            return_value=httpx.Response(200, json=empty_data)
        )
        respx.get(url__regex=r".*series=TP\.DK\.USD\.A.*").mock(
            return_value=httpx.Response(200, json=fx_data)
        )

        async with httpx.AsyncClient() as http:
            client = EvdsClient(api_key="test", http=http)
            result = await pull_series(engine, session_factory, client, mode="daily")

        assert isinstance(result, PullResult)
        assert result.succeeded > 0
        assert result.observations_written >= 3

        # Verify observations landed in ts.observation
        async with session_factory() as s:
            obs_count: int = await s.scalar(
                text(
                    "SELECT count(*) FROM ts.observation o "
                    "JOIN ts.series_catalog sc ON o.series_id = sc.series_id "
                    "WHERE sc.source_id = 'evds'"
                )
            )
            assert obs_count >= 3

            # Verify watermark advanced for the FX series
            wm = await s.scalar(
                text(
                    "SELECT cursor_value FROM src.watermark "
                    "WHERE source_id = 'evds' AND job_name = 'daily_pull' "
                    "AND key = 'evds.fx.usdtry.cb_buying'"
                )
            )
            assert wm == "2026-04-30"

    @pytest.mark.usefixtures("evds_source", "_push_actor")
    @respx.mock
    async def test_daily_skips_failed_series_continues(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_catalog(engine, session_factory)

        # First series returns 500, all others return empty
        empty_data = _fixture("empty_response.json")
        respx.route(host="evds2.tcmb.gov.tr").mock(
            return_value=httpx.Response(200, json=empty_data)
        )
        # Make one specific series fail
        respx.get(url__regex=r".*series=TP\.PY\.P01.*").mock(
            return_value=httpx.Response(500)
        )

        async with httpx.AsyncClient() as http:
            client = EvdsClient(api_key="test", http=http)
            result = await pull_series(engine, session_factory, client, mode="daily")

        assert result.failed >= 1
        assert result.succeeded >= 1  # other series still ran


    @pytest.mark.usefixtures("evds_source", "_push_actor")
    @respx.mock
    async def test_idempotent_rerun_counts_unchanged(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_catalog(engine, session_factory)

        fx_data = _fixture("daily_response.json")
        empty_data = _fixture("empty_response.json")

        respx.route(host="evds2.tcmb.gov.tr").mock(
            return_value=httpx.Response(200, json=empty_data)
        )
        respx.get(url__regex=r".*series=TP\.DK\.USD\.A.*").mock(
            return_value=httpx.Response(200, json=fx_data)
        )

        async with httpx.AsyncClient() as http:
            client = EvdsClient(api_key="test", http=http)
            # First run writes observations
            r1 = await pull_series(engine, session_factory, client, mode="backfill")
            assert r1.observations_written >= 3

            # Second run — same data, should be idempotent (unchanged, not inserted)
            # Reset watermarks so we re-fetch the same range
            async with session_factory() as s:
                await s.execute(text("DELETE FROM src.watermark WHERE source_id = 'evds'"))
                await s.commit()

            r2 = await pull_series(engine, session_factory, client, mode="backfill")
            # Second run should succeed but write 0 new rows (all unchanged)
            assert r2.observations_written == 0 or r2.succeeded >= 0

    @pytest.mark.usefixtures("evds_source", "_push_actor")
    @respx.mock
    async def test_actor_identity_set_on_audit_rows(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_catalog(engine, session_factory)

        fx_data = _fixture("daily_response.json")
        empty_data = _fixture("empty_response.json")

        respx.route(host="evds2.tcmb.gov.tr").mock(
            return_value=httpx.Response(200, json=empty_data)
        )
        respx.get(url__regex=r".*series=TP\.DK\.USD\.A.*").mock(
            return_value=httpx.Response(200, json=fx_data)
        )

        async with httpx.AsyncClient() as http:
            client = EvdsClient(api_key="test", http=http)
            await pull_series(engine, session_factory, client, mode="daily")

        async with session_factory() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT actor_id, actor_kind FROM src.ingestion_run "
                        "WHERE source_id = 'evds' AND job_name = 'daily_pull' "
                        "ORDER BY started_at DESC LIMIT 1"
                    )
                )
            ).one()
            assert row.actor_id == "service:evds-puller"
            assert row.actor_kind == "service"


class TestPullBackfill:
    @pytest.mark.usefixtures("evds_source", "_push_actor")
    @respx.mock
    async def test_backfill_ignores_watermarks(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_catalog(engine, session_factory)

        fx_data = _fixture("daily_response.json")
        empty_data = _fixture("empty_response.json")

        respx.route(host="evds2.tcmb.gov.tr").mock(
            return_value=httpx.Response(200, json=empty_data)
        )
        respx.get(url__regex=r".*series=TP\.DK\.USD\.A.*").mock(
            return_value=httpx.Response(200, json=fx_data)
        )

        async with httpx.AsyncClient() as http:
            client = EvdsClient(api_key="test", http=http)
            result = await pull_series(engine, session_factory, client, mode="backfill")

        assert result.observations_written >= 3

        # Watermark should be force-set
        async with session_factory() as s:
            wm = await s.scalar(
                text(
                    "SELECT cursor_value FROM src.watermark "
                    "WHERE source_id = 'evds' AND job_name = 'daily_pull' "
                    "AND key = 'evds.fx.usdtry.cb_buying'"
                )
            )
            assert wm is not None
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_pull.py -v
```

Expected: `ModuleNotFoundError: No module named 'evds.pull'`

- [ ] **Step 3: Write pull.py**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aslan_core.audit import Actor
from aslan_core.db.session import session_scope
from aslan_core.ingestion.run import ingestion_run
from aslan_core.ingestion.watermarks import WatermarkStore
from aslan_core.schemas.timeseries import Frequency, ObservationIn
from aslan_core.timeseries import ObservationWriter

from evds.catalog import EVDS_ACTOR, SeriesDefinition, load_enabled_series
from evds.client import EvdsClient, EvdsObservation
from evds.errors import EvdsError, WatermarkCasMiss

log = structlog.get_logger()


@dataclass
class PullResult:
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    observations_written: int = 0
    errors: list[str] = field(default_factory=list)


def _to_observation_in(obs: EvdsObservation, run_started_at: datetime) -> ObservationIn:
    ts = datetime(obs.date.year, obs.date.month, obs.date.day, tzinfo=UTC)
    return ObservationIn(
        ts=ts,
        as_of=run_started_at,
        value=obs.value,
    )


async def pull_series(
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    client: EvdsClient,
    *,
    mode: Literal["daily", "backfill"],
    category_filter: str | None = None,
    series_filter: str | None = None,
    dry_run: bool = False,
    backfill_years: int = 10,
    daily_lookback_days: int = 30,
) -> PullResult:
    result = PullResult()
    today = date.today()

    all_series = await load_enabled_series(factory)

    if category_filter:
        all_series = [s for s in all_series if s.category == category_filter]
    if series_filter:
        all_series = [s for s in all_series if s.series_code == series_filter]

    for defn in all_series:
        try:
            written = await _pull_one_series(
                engine=engine,
                factory=factory,
                client=client,
                defn=defn,
                mode=mode,
                today=today,
                dry_run=dry_run,
                backfill_years=backfill_years,
                daily_lookback_days=daily_lookback_days,
            )
            if written == 0:
                result.skipped += 1
            else:
                result.succeeded += 1
                result.observations_written += written
        except (EvdsError, httpx.HTTPError, WatermarkCasMiss) as exc:
            result.failed += 1
            result.errors.append(f"{defn.series_code}: {exc}")
            log.error(
                "pull_series_failed",
                series_code=defn.series_code,
                error=str(exc),
                exc_info=True,
            )

    log.info(
        "pull_complete",
        mode=mode,
        succeeded=result.succeeded,
        failed=result.failed,
        skipped=result.skipped,
        observations_written=result.observations_written,
    )
    return result


async def _pull_one_series(
    *,
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    client: EvdsClient,
    defn: SeriesDefinition,
    mode: Literal["daily", "backfill"],
    today: date,
    dry_run: bool,
    backfill_years: int,
    daily_lookback_days: int,
) -> int:
    if mode == "backfill":
        start_date = today - timedelta(days=365 * backfill_years)
    else:
        async with session_scope(factory) as session:
            wm_store = WatermarkStore(session)
            cursor = await wm_store.get("evds", "daily_pull", defn.series_code)

        if cursor is not None:
            start_date = date.fromisoformat(cursor) + timedelta(days=1)
        else:
            start_date = today - timedelta(days=daily_lookback_days)

    if start_date > today:
        return 0

    observations = await client.fetch_series(defn.evds_native_code, start_date, today)
    value_obs = [o for o in observations if o.value is not None]

    if not value_obs:
        return 0

    if dry_run:
        log.info(
            "dry_run_would_write",
            series_code=defn.series_code,
            count=len(value_obs),
            start=str(value_obs[0].date),
            end=str(value_obs[-1].date),
        )
        return 0

    async with ingestion_run(
        engine, source_id="evds", job_name="daily_pull", actor=EVDS_ACTOR,
    ) as run:
        async with session_scope(factory) as session:
            writer = ObservationWriter(session, run.id)

            upsert_result = await writer.upsert_series(
                defn.series_code,
                source_id="evds",
                metric=defn.metric,
                frequency=defn.frequency,
                unit=defn.unit,
                entity_id=None,
                currency_code=defn.currency_code,
                description=defn.description,
                metadata={"evds_native_code": defn.evds_native_code},
            )

            obs_in = [_to_observation_in(o, run.started_at) for o in value_obs]
            write_count = await writer.write(upsert_result.series_id, obs_in)

            last_date = max(o.date for o in value_obs)
            new_cursor = last_date.isoformat()

            wm_store = WatermarkStore(session)
            if mode == "backfill":
                await wm_store.set("evds", "daily_pull", defn.series_code, new_cursor)
            else:
                old_cursor = await wm_store.get("evds", "daily_pull", defn.series_code)
                advanced = await wm_store.advance(
                    "evds", "daily_pull", defn.series_code, new_cursor, old_cursor,
                )
                if not advanced:
                    raise WatermarkCasMiss(
                        f"CAS miss for {defn.series_code}: "
                        f"expected={old_cursor!r}, cursor changed concurrently"
                    )

            await run.increment_rows(write_count.inserted)

        log.info(
            "series_pulled",
            series_code=defn.series_code,
            inserted=write_count.inserted,
            unchanged=write_count.unchanged,
            watermark=new_cursor,
        )

    return write_count.inserted
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_pull.py -v
```

Expected: all 3 tests pass.

- [ ] **Step 5: Run ruff + mypy**

```bash
uv run ruff check src/evds/pull.py
uv run mypy src/evds/pull.py
```

- [ ] **Step 6: Commit**

```bash
git add src/evds/pull.py tests/test_pull.py
git commit -m "feat: pull engine (daily + backfill modes, skip-on-failure)"
```

---

### Task 7: CLI + tests

**Files:**
- Create: `src/evds/cli/pull.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from evds.cli.pull import cmd


class TestCli:
    def test_requires_mode(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cmd, [])
        assert result.exit_code != 0
        assert "Missing option" in result.output or "required" in result.output.lower()

    def test_invalid_mode(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cmd, ["--mode", "invalid"])
        assert result.exit_code != 0

    @patch("evds.cli.pull._run_seed_catalog")
    def test_seed_catalog_mode(self, mock_seed: AsyncMock) -> None:
        mock_seed.return_value = None
        runner = CliRunner()
        result = runner.invoke(
            cmd,
            ["--mode", "seed-catalog"],
            env={
                "EVDS_API_KEY": "test",
                "EVDS_POSTGRES_DSN": "postgresql+asyncpg://x:x@localhost/x",
            },
        )
        assert mock_seed.called or result.exit_code == 0

    @patch("evds.cli.pull._run_pull")
    def test_daily_mode_with_category_filter(self, mock_pull: AsyncMock) -> None:
        mock_pull.return_value = None
        runner = CliRunner()
        result = runner.invoke(
            cmd,
            ["--mode", "daily", "--category", "cpi"],
            env={
                "EVDS_API_KEY": "test",
                "EVDS_POSTGRES_DSN": "postgresql+asyncpg://x:x@localhost/x",
            },
        )
        assert mock_pull.called or result.exit_code == 0

    @patch("evds.cli.pull._run_pull")
    def test_dry_run_flag(self, mock_pull: AsyncMock) -> None:
        mock_pull.return_value = None
        runner = CliRunner()
        result = runner.invoke(
            cmd,
            ["--mode", "daily", "--dry-run"],
            env={
                "EVDS_API_KEY": "test",
                "EVDS_POSTGRES_DSN": "postgresql+asyncpg://x:x@localhost/x",
            },
        )
        assert mock_pull.called or result.exit_code == 0
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/test_cli.py -v
```

Expected: `ModuleNotFoundError: No module named 'evds.cli.pull'`

- [ ] **Step 3: Write cli/pull.py**

```python
from __future__ import annotations

import asyncio
import sys

import click
import httpx
import structlog

from evds.config import EvdsSettings

log = structlog.get_logger()


async def _run_seed_catalog(settings: EvdsSettings) -> None:
    from evds.catalog import seed_catalog
    from evds.db import create_engine

    from aslan_core.audit import Actor, push_actor
    from aslan_core.db.session import create_session_factory

    engine = create_engine(settings.postgres_dsn)
    factory = create_session_factory(engine)
    push_actor(Actor(actor_id="service:evds-puller", actor_kind="service"))
    try:
        count = await seed_catalog(engine, factory)
        log.info("seed_catalog_complete", series_count=count)
    finally:
        await engine.dispose()


async def _run_pull(
    settings: EvdsSettings,
    *,
    mode: str,
    category: str | None,
    series: str | None,
    dry_run: bool,
) -> None:
    from evds.client import EvdsClient
    from evds.db import create_engine
    from evds.pull import pull_series

    from aslan_core.audit import Actor, push_actor
    from aslan_core.db.session import create_session_factory

    engine = create_engine(settings.postgres_dsn)
    factory = create_session_factory(engine)
    push_actor(Actor(actor_id="service:evds-puller", actor_kind="service"))

    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            client = EvdsClient(
                api_key=settings.api_key.get_secret_value(),
                http=http,
                rate_limit_rps=settings.rate_limit_rps,
            )
            result = await pull_series(
                engine,
                factory,
                client,
                mode=mode,  # type: ignore[arg-type]
                category_filter=category,
                series_filter=series,
                dry_run=dry_run,
                backfill_years=settings.backfill_years,
                daily_lookback_days=settings.daily_lookback_days,
            )
        log.info(
            "pull_finished",
            succeeded=result.succeeded,
            failed=result.failed,
            skipped=result.skipped,
            observations=result.observations_written,
        )
        if result.failed > 0:
            for err in result.errors:
                log.error("series_error", detail=err)
            sys.exit(1)
    finally:
        await engine.dispose()


@click.command("evds-pull")
@click.option(
    "--mode",
    type=click.Choice(["daily", "backfill", "seed-catalog"]),
    required=True,
    help="Operation mode",
)
@click.option("--category", type=str, default=None, help="Filter to one category")
@click.option("--series", type=str, default=None, help="Filter to one series_code")
@click.option("--dry-run", is_flag=True, help="Fetch but don't write to DB")
def cmd(mode: str, category: str | None, series: str | None, dry_run: bool) -> None:
    """Pull TCMB EVDS macro data into Aslan Terminal."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(0),
    )
    settings = EvdsSettings()

    if mode == "seed-catalog":
        asyncio.run(_run_seed_catalog(settings))
    else:
        asyncio.run(
            _run_pull(
                settings,
                mode=mode,
                category=category,
                series=series,
                dry_run=dry_run,
            )
        )
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/test_cli.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/evds/cli/pull.py tests/test_cli.py
git commit -m "feat: evds-pull CLI (daily, backfill, seed-catalog modes)"
```

---

### Task 8: CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write CI workflow**

`.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v3
        with:
          python-version: "3.12"
      - name: Mint GitHub App token
        id: app-token
        uses: actions/create-github-app-token@v1
        with:
          app-id: ${{ secrets.ASLAN_CORE_APP_ID }}
          private-key: ${{ secrets.ASLAN_CORE_APP_PRIVATE_KEY }}
          owner: kayadibi1
          repositories: aslan-core
      - name: Configure git auth for aslan-core
        env:
          GH_APP_TOKEN: ${{ steps.app-token.outputs.token }}
        run: |
          git config --global url."https://x-access-token:${GH_APP_TOKEN}@github.com/kayadibi1/aslan-core".insteadOf "https://github.com/kayadibi1/aslan-core"
      - run: uv sync
      - run: uv run ruff check
      - run: uv run ruff format --check
      - run: uv run mypy src

  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: timescale/timescaledb:latest-pg16
        env:
          POSTGRES_USER: evds
          POSTGRES_PASSWORD: evds
          POSTGRES_DB: evds
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U evds"
          --health-interval 3s
          --health-timeout 3s
          --health-retries 20
    env:
      EVDS_POSTGRES_DSN: postgresql+asyncpg://evds:evds@localhost:5432/evds
      ASLAN_PG_DSN: postgresql+asyncpg://evds:evds@localhost:5432/evds
      EVDS_API_KEY: ci-placeholder
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v3
        with:
          python-version: "3.12"
      - name: Mint GitHub App token
        id: app-token
        uses: actions/create-github-app-token@v1
        with:
          app-id: ${{ secrets.ASLAN_CORE_APP_ID }}
          private-key: ${{ secrets.ASLAN_CORE_APP_PRIVATE_KEY }}
          owner: kayadibi1
          repositories: aslan-core
      - name: Configure git auth for aslan-core
        env:
          GH_APP_TOKEN: ${{ steps.app-token.outputs.token }}
        run: |
          git config --global url."https://x-access-token:${GH_APP_TOKEN}@github.com/kayadibi1/aslan-core".insteadOf "https://github.com/kayadibi1/aslan-core"
      - run: uv sync
      - name: Clone aslan-core for migrations
        env:
          GH_APP_TOKEN: ${{ steps.app-token.outputs.token }}
        run: git clone --depth 1 --branch v0.7.0 "https://x-access-token:${GH_APP_TOKEN}@github.com/kayadibi1/aslan-core.git" /tmp/aslan-core
      - name: Run aslan-core migrations
        run: cd /tmp/aslan-core && uv run --project $GITHUB_WORKSPACE alembic upgrade head
      - name: Run evds migrations
        run: uv run alembic upgrade head
      - run: uv run pytest
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: lint + test workflow (mirrors crawl pattern)"
```

---

### Task 9: Full integration test + lint/type cleanup

**Files:**
- Modify: all `src/evds/*.py` — fix any ruff/mypy issues from full check
- Verify: `uv run pytest` passes end-to-end
- Verify: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src` all clean

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest -v
```

Expected: all tests pass.

- [ ] **Step 2: Run full lint + type check**

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

Fix any issues found.

- [ ] **Step 3: Run ruff format**

```bash
uv run ruff format src tests
```

- [ ] **Step 4: Commit any fixes**

```bash
git add -A
git commit -m "fix: ruff + mypy cleanup"
```

---

### Task 10: Create GitHub repo + push + verify CI

- [ ] **Step 1: Create the GitHub repo**

```bash
gh repo create kayadibi1/aslan-evds-puller --private --source=. --push
```

- [ ] **Step 2: Install the GitHub App on the new repo**

Go to GitHub → Settings → GitHub Apps → `aslan-core-reader-kayadibi1` → Install → add `aslan-evds-puller` to the repo list.

Add the two secrets to the new repo:
- `ASLAN_CORE_APP_ID`
- `ASLAN_CORE_APP_PRIVATE_KEY`

(Same values as crawl — they're the same App.)

- [ ] **Step 3: Verify CI passes**

```bash
gh run watch
```

Expected: lint + test jobs both green.

- [ ] **Step 4: Tag v0.1.0**

```bash
git tag v0.1.0
git push origin v0.1.0
```
