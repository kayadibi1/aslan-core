# Standardized Financials REST API — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a FastAPI REST API exposing standardized financials, CPI-normalized comparisons, and quality scores with JWT auth + refresh token rotation.

**Architecture:** Query layer (`aslan_core.query.*`) wraps SQL with typed enums and Principal auth context. API layer (`aslan_core.api.*`) is a thin FastAPI wrapper with JWT middleware, rate limiting, and custom error handling. Auth uses bcrypt + HS256 JWT + refresh token families with replay detection.

**Tech Stack:** Python 3.12, FastAPI 0.115+, SQLAlchemy 2.0 (async), python-jose, passlib[bcrypt], uvicorn, Pydantic v2.

**Spec:** `docs/superpowers/specs/2026-05-03-financials-api-design.md` (rev 2 — codex-reviewed)

---

## Phase 1: Foundation (Tasks 1-3)

### Task 1: Migration 0033 — auth schema

**Files:**
- Create: `src/aslan_core/db/migrations/versions/20260503_2000_0033_auth_schema.py`
- Test: `tests/integration/test_migration_0033.py`

- [ ] **Step 1: Write the migration**

```python
"""auth schema — user accounts + refresh tokens.

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-03 20:00:00
"""
from __future__ import annotations
from collections.abc import Sequence
from alembic import op

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS auth")
    op.execute("""
        CREATE TABLE auth.user (
            user_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'user'
                CHECK (role IN ('user', 'admin')),
            is_active     BOOLEAN NOT NULL DEFAULT TRUE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE auth.refresh_token (
            token_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id       UUID NOT NULL
                REFERENCES auth.user(user_id) ON DELETE CASCADE,
            token_family  UUID NOT NULL,
            token_hash    CHAR(64) NOT NULL,
            expires_at    TIMESTAMPTZ NOT NULL,
            revoked       BOOLEAN NOT NULL DEFAULT FALSE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX rt_user_id ON auth.refresh_token(user_id)")
    op.execute("CREATE INDEX rt_family ON auth.refresh_token(token_family)")

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auth.refresh_token")
    op.execute("DROP TABLE IF EXISTS auth.user")
    op.execute("DROP SCHEMA IF EXISTS auth")
```

- [ ] **Step 2: Write integration tests** — table exists, columns correct, role CHECK, unique email
- [ ] **Step 3: Run tests, commit:** `feat(migration): auth schema (0033)`

### Task 2: Query schemas + enums

**Files:**
- Create: `src/aslan_core/query/__init__.py`
- Create: `src/aslan_core/query/schemas.py`

- [ ] **Step 1: Create the query package and schemas**

All enums (`StatementType`, `PeriodType`, `RestatementBasis`, `Consolidation`), `Principal` dataclass, and all response models (`EntitySummary`, `PeriodData`, `MetricPoint`, `TimeseriesPoint`, `QualityScorePublic`, `QualityCheckPublic`, `CanonicalLineInfo`) from spec §5.4, §5.5, §7.

- [ ] **Step 2: Write tests** — enum values match spec, Principal is frozen, Pydantic models serialize correctly, Decimal fields not float
- [ ] **Step 3: Commit:** `feat: query schemas + enums + Principal`

### Task 3: Dependencies + pyproject.toml

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `[api]` optional extra**

```toml
api = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "python-jose[cryptography]>=3.3",
    "passlib[bcrypt]>=1.7",
]
```

- [ ] **Step 2: Add mypy overrides** for jose, passlib if needed
- [ ] **Step 3: Commit:** `chore: add [api] optional dependency extra`

---

## Phase 2: Query Layer (Tasks 4-7)

### Task 4: Query — list_entities + get_entity_quality

**Files:**
- Create: `src/aslan_core/query/entity.py`
- Test: `tests/integration/test_query_entity.py`

- [ ] **Step 1: Write failing tests** — `list_entities` with search, pagination, bounds; `get_entity_quality` with public check output (no raw JSONB internals)
- [ ] **Step 2: Implement** — SQL via `text()` with bind params, returns Pydantic models. `list_entities` joins `ref.entity` with `ts.canonical_financial` aggregate (when `has_financials=True`). `get_entity_quality` reads `ts.entity_quality_score`, strips internal diagnostics into `QualityCheckPublic`.
- [ ] **Step 3: Run tests, commit:** `feat: query layer — list_entities + get_entity_quality`

### Task 5: Query — get_entity_financials

**Files:**
- Create: `src/aslan_core/query/financials.py`
- Test: `tests/integration/test_query_financials.py`

- [ ] **Step 1: Write failing tests** — filter by statement_type, period_type, restatement_basis, consolidation, limit enforcement. Seed canonical_financial rows.
- [ ] **Step 2: Implement** — Query `ts.canonical_financial` with bind params, group by period_end into `PeriodData` with `lines: dict[str, Decimal | None]`. DISTINCT ON for PIT collapse (latest mapping_version per canonical key).
- [ ] **Step 3: Run tests, commit:** `feat: query layer — get_entity_financials`

### Task 6: Query — compare_metric + get_metric_timeseries

**Files:**
- Modify: `src/aslan_core/query/financials.py`
- Test: `tests/integration/test_query_financials.py` (add tests)

- [ ] **Step 1: Write failing tests** — `compare_metric` with 2 entities, deduplication, max 10 enforcement. `get_metric_timeseries` with both-bases response (restatement_basis=None).
- [ ] **Step 2: Implement** — `compare_metric` uses `WHERE entity_id = ANY(:ids)` with bound param. `get_metric_timeseries` with `restatement_basis IS NULL` returns both bases via self-join or conditional aggregation.
- [ ] **Step 3: Run tests, commit:** `feat: query layer — compare_metric + get_metric_timeseries`

### Task 7: Query — list_canonical_lines (catalog)

**Files:**
- Create: `src/aslan_core/query/catalog.py`
- Test: `tests/unit/test_query_catalog.py`

- [ ] **Step 1: Write failing tests** — returns all lines, filterable by statement_type
- [ ] **Step 2: Implement** — Reads canonical_lines.yaml (loaded once at module import), returns `list[CanonicalLineInfo]`. No DB access.
- [ ] **Step 3: Run tests, commit:** `feat: query layer — list_canonical_lines`

---

## Phase 3: Auth (Tasks 8-10)

### Task 8: JWT utilities + password hashing

**Files:**
- Create: `src/aslan_core/api/auth.py`
- Test: `tests/unit/test_auth.py`

- [ ] **Step 1: Write failing tests** — `create_access_token` contains all required claims (iss, aud, sub, exp, nbf, iat, jti, typ). `verify_access_token` rejects expired, wrong iss, wrong aud, missing claims, typ != "access". `hash_password` + `verify_password` round-trip. `hash_refresh_token` produces SHA-256 hex.
- [ ] **Step 2: Implement**

```python
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from jose import JWTError, jwt
from passlib.context import CryptContext
import hashlib

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

JWT_ALGORITHM = "HS256"
ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=7)

def create_access_token(user_id: UUID, role: str, secret: str) -> str: ...
def verify_access_token(token: str, secret: str) -> dict: ...
def create_refresh_token() -> tuple[str, str]:
    """Returns (raw_token, hashed_token)."""
def hash_password(password: str) -> str: ...
def verify_password(plain: str, hashed: str) -> bool: ...
```

- [ ] **Step 3: Run tests, commit:** `feat: JWT utilities + password hashing`

### Task 9: Auth endpoints (register, login, refresh)

**Files:**
- Create: `src/aslan_core/api/routes/__init__.py`
- Modify: `src/aslan_core/api/auth.py` (add route functions)
- Test: `tests/integration/test_api_auth.py`

- [ ] **Step 1: Write failing tests** — register with invite_code succeeds, register without → 403, register duplicate email → 400, login succeeds → tokens returned, login bad password → 401 (uniform message), refresh rotates tokens (old rejected, new works), refresh reuse → family revoked, inactive user → 401, password < 10 chars → 400.
- [ ] **Step 2: Implement** — FastAPI router with `POST /auth/register`, `/auth/login`, `/auth/refresh`. Registration checks invite_code against `ASLAN_INVITE_CODE` env var (simple shared secret for v1). Refresh implements §4.6 rotation with family tracking.
- [ ] **Step 3: Run tests, commit:** `feat: auth endpoints — register, login, refresh`

### Task 10: Auth dependencies (current_user, rate limiter)

**Files:**
- Create: `src/aslan_core/api/deps.py`
- Create: `src/aslan_core/api/middleware.py`
- Test: `tests/unit/test_api_deps.py`

- [ ] **Step 1: Write failing tests** — `get_current_user` extracts Principal from valid token, rejects invalid/expired, checks is_active. Rate limiter returns 429 after threshold.
- [ ] **Step 2: Implement**

```python
# deps.py
async def get_session() -> AsyncGenerator[AsyncSession, None]: ...
async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> Principal: ...
```

```python
# middleware.py — custom exception handlers
async def http_exception_handler(request, exc): ...
async def validation_exception_handler(request, exc): ...
```

Rate limiter: in-memory sliding window dict keyed by (user_id, endpoint_group). Reset on window expiry.

- [ ] **Step 3: Run tests, commit:** `feat: auth dependencies + error middleware + rate limiter`

---

## Phase 4: API Routes (Tasks 11-14)

### Task 11: FastAPI app factory

**Files:**
- Create: `src/aslan_core/api/__init__.py`
- Test: `tests/unit/test_api_app.py`

- [ ] **Step 1: Implement `create_api_app()`**

```python
from fastapi import FastAPI
def create_api_app() -> FastAPI:
    app = FastAPI(title="Aslan Financial API", version="0.10.0")
    # Register routers, exception handlers, startup events
    return app
```

- [ ] **Step 2: Test** — app creates, OpenAPI schema accessible, health endpoint works
- [ ] **Step 3: Commit:** `feat: FastAPI app factory`

### Task 12: Entity routes

**Files:**
- Create: `src/aslan_core/api/routes/entities.py`
- Test: `tests/integration/test_api_entities.py`

- [ ] **Step 1: Write failing tests** using `TestClient` — `/entities` returns paginated list, `/entities/{id}/financials` returns periods with Decimal values, `/entities/{id}/quality` returns public check output. Auth required (401 without token). Empty results → 200 with empty array. Invalid UUID → 400.
- [ ] **Step 2: Implement** — thin routes calling `aslan_core.query.entity.*` and `aslan_core.query.financials.*` with `Depends(get_current_user)`.
- [ ] **Step 3: Run tests, commit:** `feat: entity API routes`

### Task 13: Financial routes

**Files:**
- Create: `src/aslan_core/api/routes/financials.py`
- Test: `tests/integration/test_api_financials.py`

- [ ] **Step 1: Write failing tests** — `/financials/compare` with comma-sep entity_ids, max 10 enforcement, deduplication. `/financials/timeseries` with both-bases response. `canonical_code` validated against manifest. limit > 100 → 400.
- [ ] **Step 2: Implement** — parse entity_ids from comma-sep string, validate canonical_code against loaded manifest, call query functions.
- [ ] **Step 3: Run tests, commit:** `feat: financial API routes`

### Task 14: Catalog route

**Files:**
- Create: `src/aslan_core/api/routes/catalog.py`
- Test: `tests/integration/test_api_catalog.py`

- [ ] **Step 1: Write failing tests** — `/canonical-lines` returns all lines, filterable by statement_type. No auth required for catalog (public metadata).
- [ ] **Step 2: Implement** — calls `aslan_core.query.catalog.list_canonical_lines()`.
- [ ] **Step 3: Run tests, commit:** `feat: catalog API route`

---

## Phase 5: CLI + Deploy (Tasks 15-17)

### Task 15: CLI command `aslan api serve`

**Files:**
- Create: `src/aslan_core/cli/api.py`
- Modify: `src/aslan_core/cli/main.py` (register `api` group)
- Test: `tests/unit/test_cli_api.py`

- [ ] **Step 1: Implement**

```python
@click.group()
def api() -> None:
    """API server operations."""

@api.command("serve")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8600, type=int)
def serve(host: str, port: int) -> None:
    """Start the Financial API server."""
    import os
    secret = os.environ.get("ASLAN_JWT_SECRET", "")
    if len(secret) < 32:
        raise click.UsageError("ASLAN_JWT_SECRET must be >= 32 chars")
    import uvicorn
    from aslan_core.api import create_api_app
    app = create_api_app()
    uvicorn.run(app, host=host, port=port)
```

- [ ] **Step 2: Register in main.py:** `cli.add_command(api)`
- [ ] **Step 3: Test** — help text, JWT secret validation
- [ ] **Step 4: Commit:** `feat: aslan api serve CLI command`

### Task 16: Lint + type check + full test suite

- [ ] **Step 1: `uv run ruff check src/aslan_core/query/ src/aslan_core/api/`**
- [ ] **Step 2: `uv run ruff format --check`**
- [ ] **Step 3: `uv run mypy src/aslan_core/query/ src/aslan_core/api/`**
- [ ] **Step 4: `uv run pytest tests/ -v --tb=short`**
- [ ] **Step 5: Fix issues, commit:** `fix: lint + type cleanup`

### Task 17: Version bump + deploy

- [ ] **Step 1: Bump version to 0.10.0**
- [ ] **Step 2: Tag v0.10.0, push**
- [ ] **Step 3: Add API service to docker-compose.yml on Hetzner**
- [ ] **Step 4: Apply migration 0033**
- [ ] **Step 5: Create first admin user** via direct SQL or a seed script
- [ ] **Step 6: Start API service, verify endpoints via curl**
- [ ] **Step 7: Commit deploy notes**
