# Standardized Financials REST API

**Date:** 2026-05-03
**Status:** Draft
**Repo:** aslan-core
**Depends on:** v0.9.0 (ts.canonical_financial, ts.entity_quality_score)

## 1. Problem

68,832 canonical financial rows and 478 quality scores exist in
production but have no consumption layer. The data is inaccessible
to users, dashboards, or an AI query layer. Bloomberg provides BDH
and FA interfaces for querying financial data; we have nothing.

## 2. Goal

Build a FastAPI REST API in aslan-core that exposes standardized
financials, CPI-normalized comparisons, and quality scores via
JSON endpoints with JWT authentication. This serves as the interface
for both human consumers (via dashboard/frontend) and the AI query
layer (direct library calls or HTTP).

**Surpasses Bloomberg:**
- Both restatement bases (as_reported + cpi_normalized) in one call
- Quality scores on every response — no Bloomberg equivalent
- Cross-entity comparison in one endpoint
- Source traceability available via source_contributions
- Full OpenAPI auto-documentation
- Sub-second response times (direct SQL, no analyst lag)

## 3. Architecture

Two services in aslan-core, separate processes:
- **Dashboard** (existing): FastHTML on port 8585, operator UI
- **API** (new): FastAPI on port 8600, JSON REST + JWT auth

Both share the same database. The API wraps `aslan_core.query.*`
library functions which can also be called directly by the AI service
without HTTP overhead.

## 4. Authentication

### 4.1 Auth flow

1. `POST /auth/register` — create account (email + bcrypt-hashed
   password)
2. `POST /auth/login` — returns JWT access token (15min) + refresh
   token (7d)
3. `POST /auth/refresh` — exchange refresh token for new access token
4. All other endpoints require `Authorization: Bearer <access_token>`

### 4.2 Token details

| Parameter | Value |
|---|---|
| Password hashing | bcrypt via passlib |
| JWT algorithm | HS256 |
| JWT secret | `ASLAN_JWT_SECRET` env var (required at startup) |
| Access token TTL | 15 minutes |
| Refresh token TTL | 7 days |
| Refresh token storage | `auth.refresh_token` table, token hashed with SHA-256 |

### 4.3 Schema (migration 0033)

```sql
CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE auth.user (
    user_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE auth.refresh_token (
    token_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES auth.user(user_id) ON DELETE CASCADE,
    token_hash  CHAR(64) NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX rt_user_id ON auth.refresh_token(user_id);
```

## 5. Query layer (`aslan_core.query`)

Library functions that wrap SQL queries and return Pydantic models.
Callable directly by the AI service or indirectly via the API.

### 5.1 financials.py

```python
async def get_entity_financials(
    session: AsyncSession,
    entity_id: UUID,
    *,
    statement_type: str | None = None,
    period_type: str | None = None,
    restatement_basis: str = "as_reported",
    consolidation: str = "consolidated",
    limit: int = 20,
) -> list[CanonicalFinancialRow]:
    """Full canonical financials for one entity.

    Returns rows grouped by period, filterable by statement type and
    period type. Each row includes value, source_contributions,
    quality_flags, and metadata.
    """
```

```python
async def compare_metric(
    session: AsyncSession,
    entity_ids: list[UUID],
    canonical_code: str,
    *,
    restatement_basis: str = "cpi_normalized",
    period_type: str = "q",
    consolidation: str = "consolidated",
    limit: int = 8,
) -> dict[UUID, list[MetricPoint]]:
    """One metric across multiple entities.

    Returns keyed by entity_id, each with a time series of MetricPoints.
    Default to cpi_normalized for cross-entity comparison.
    """
```

```python
async def get_metric_timeseries(
    session: AsyncSession,
    entity_id: UUID,
    canonical_code: str,
    *,
    restatement_basis: str | None = None,
    period_type: str = "q",
    consolidation: str = "consolidated",
    limit: int = 20,
) -> list[TimeseriesPoint]:
    """One entity + one metric over time.

    If restatement_basis is None, returns both bases side by side
    for easy comparison.
    """
```

### 5.2 entity.py

```python
async def list_entities(
    session: AsyncSession,
    *,
    search: str | None = None,
    has_financials: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[EntitySummary], int]:
    """List entities with optional search and pagination.

    Returns (items, total_count). When has_financials=True, only
    returns entities that have canonical financial data.
    """
```

```python
async def get_entity_quality(
    session: AsyncSession,
    entity_id: UUID,
    *,
    restatement_basis: str = "as_reported",
    limit: int = 10,
) -> list[QualityScoreDetail]:
    """Quality scores + check details for one entity.

    Returns most recent periods first with full checks JSONB.
    """
```

### 5.3 catalog.py

```python
async def list_canonical_lines(
    statement_type: str | None = None,
) -> list[CanonicalLineInfo]:
    """List available canonical codes with descriptions.

    Reads from the YAML manifest (loaded once at startup), not the
    database. Filterable by statement type.
    """
```

## 6. API endpoints

### 6.1 Auth

| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/auth/register` | `{ email, password }` | `{ user_id, email }` |
| POST | `/auth/login` | `{ email, password }` | `{ access_token, refresh_token, token_type, expires_in }` |
| POST | `/auth/refresh` | `{ refresh_token }` | `{ access_token, token_type, expires_in }` |

### 6.2 Entities

| Method | Path | Query params | Response |
|---|---|---|---|
| GET | `/entities` | `search`, `has_financials`, `limit`, `offset` | `{ items: [EntitySummary], total, offset, limit }` |
| GET | `/entities/{id}/financials` | `statement_type`, `period_type`, `restatement_basis`, `consolidation`, `limit` | `{ entity_id, legal_name, periods: [PeriodData] }` |
| GET | `/entities/{id}/quality` | `restatement_basis`, `limit` | `{ entity_id, legal_name, scores: [QualityScore] }` |

### 6.3 Financials

| Method | Path | Query params | Response |
|---|---|---|---|
| GET | `/financials/compare` | `entity_ids` (comma-sep), `canonical_code`, `restatement_basis`, `period_type`, `consolidation`, `limit` | `{ canonical_code, description, entities: { uuid: [MetricPoint] } }` |
| GET | `/financials/timeseries` | `entity_id`, `canonical_code`, `restatement_basis`, `period_type`, `consolidation`, `limit` | `{ entity_id, canonical_code, points: [TimeseriesPoint] }` |

### 6.4 Catalog

| Method | Path | Query params | Response |
|---|---|---|---|
| GET | `/canonical-lines` | `statement_type` | `{ lines: [CanonicalLineInfo] }` |

## 7. Response models

```python
class EntitySummary(BaseModel):
    entity_id: UUID
    legal_name: str
    ticker: str | None
    latest_period: date | None
    avg_quality_score: int | None
    canonical_line_count: int

class PeriodData(BaseModel):
    period_end: date
    period_type: str
    consolidation: str
    restatement_basis: str
    lines: dict[str, float | None]
    quality_score: int | None

class MetricPoint(BaseModel):
    period_end: date
    period_type: str
    value: float | None
    restatement_basis: str

class TimeseriesPoint(BaseModel):
    period_end: date
    period_type: str
    as_reported: float | None
    cpi_normalized: float | None
    cpi_base_date: date | None

class QualityScore(BaseModel):
    period_end: date
    period_type: str
    consolidation: str
    score: int
    insufficient_data: bool
    checks: dict[str, Any]

class CanonicalLineInfo(BaseModel):
    canonical_code: str
    statement_type: str
    description: str
    computed: bool
    required: bool
    monetary: bool
```

## 8. Error responses

All errors return JSON with consistent shape:

```json
{
  "detail": "Entity not found",
  "status_code": 404
}
```

| Status | When |
|---|---|
| 400 | Invalid query params (bad UUID, unknown canonical_code) |
| 401 | Missing/expired/invalid token |
| 404 | Entity not found, no data for filters |
| 422 | Request validation error (FastAPI default) |
| 500 | Internal server error |

## 9. File structure


```
aslan-core/src/aslan_core/
  query/
    __init__.py
    financials.py      # get_entity_financials, compare_metric, get_metric_timeseries
    entity.py          # list_entities, get_entity_quality
    catalog.py         # list_canonical_lines
    schemas.py         # Pydantic models shared by query + API
  api/
    __init__.py        # FastAPI app factory: create_api_app()
    auth.py            # register, login, refresh endpoints + JWT utils
    deps.py            # Depends() for session, current_user
    routes/
      __init__.py
      entities.py      # /entities endpoints
      financials.py    # /financials endpoints
      catalog.py       # /canonical-lines endpoint
```

## 10. CLI + deployment

### 10.1 CLI

```
aslan api serve [--host HOST] [--port PORT]
```

Default: `--host 127.0.0.1 --port 8600`. Requires
`ASLAN_JWT_SECRET` env var.

### 10.2 Docker Compose

New service in `infra/deploy/docker-compose.yml`:

```yaml
  api:
    build:
      context: ../..
      dockerfile: infra/deploy/Dockerfile
    environment:
      ASLAN_PG_DSN: "postgresql+asyncpg://aslan:aslan@postgres:5432/aslan"
      ASLAN_JWT_SECRET: "${ASLAN_JWT_SECRET}"
    ports:
      - "127.0.0.1:8600:8600"
    entrypoint: ["aslan", "api", "serve", "--host", "0.0.0.0", "--port", "8600"]
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped
```

Access via SSH tunnel: `ssh -L 8600:127.0.0.1:8600 emersus@37.27.112.76`

## 11. Dependencies

New in aslan-core `pyproject.toml`:

```toml
[project.optional-dependencies]
api = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "python-jose[cryptography]>=3.3",
    "passlib[bcrypt]>=1.7",
]
```

Optional extra so base install isn't bloated. The API service
installs with `pip install aslan-core[api]`.

## 12. Testing

### 12.1 Query layer unit tests

- `test_get_entity_financials` — seed canonical rows, verify
  filtering by statement_type, period_type, restatement_basis
- `test_compare_metric` — seed 2 entities, verify cross-entity
  response keyed by UUID
- `test_get_metric_timeseries` — verify both-bases response when
  restatement_basis=None
- `test_list_entities` — search by name, pagination
- `test_get_entity_quality` — verify checks JSONB included

### 12.2 API integration tests

Using FastAPI `TestClient`:
- Auth flow: register → login → access protected endpoint → refresh
- Expired token rejection (mock time)
- Invalid credentials → 401
- Each endpoint returns correct shape
- Query parameter validation (invalid UUID, unknown canonical_code)

### 12.3 Auth tests

- Password hashed with bcrypt (not stored plaintext)
- Refresh token hashed in DB
- Revoked refresh token rejected
- Inactive user can't login

## 13. Migrations

| Migration | What |
|---|---|
| 0033 | `auth.user` + `auth.refresh_token` tables |

## 14. Version target

aslan-core v0.10.0 (migration 0033 + query module + API service).
