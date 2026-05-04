# Standardized Financials REST API

**Date:** 2026-05-03
**Status:** Draft (rev 2 — incorporates codex adversarial review)
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
JSON endpoints with JWT authentication and role-based authorization.
This serves as the interface for both human consumers (via
dashboard/frontend) and the AI query layer (direct library calls
or HTTP).

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
— but all query functions require an authenticated principal (§5.4).

## 4. Authentication + Authorization

### 4.1 Auth flow

1. `POST /auth/register` — create account (email + password).
   **Registration is gated**: requires a valid `invite_code` or
   `is_admin` on the creating user. No open self-registration.
2. `POST /auth/login` — returns JWT access token (15min) + refresh
   token (7d, rotated on use)
3. `POST /auth/refresh` — exchange refresh token for new access +
   refresh token pair. Old refresh token is revoked atomically.
4. All other endpoints require `Authorization: Bearer <access_token>`

### 4.2 Authorization model (C-1)

All financial data is accessible to any authenticated, active user
for v1. This is intentional — the data is public (KAP filings are
public disclosures). The authorization gate is:

1. **Valid JWT** with correct `iss`, `aud`, `sub`, `exp`, `nbf` claims
2. **Active user** — `is_active = true` checked on every request
   (not just at token issue time)
3. **Role**: `user` or `admin`. Admins can register new users.

Future: per-entity access grants, tenant scoping, API key tiers.

### 4.3 Token details

| Parameter | Value |
|---|---|
| Password hashing | bcrypt via passlib |
| JWT algorithm | HS256, `algorithms=["HS256"]` explicit |
| JWT secret | `ASLAN_JWT_SECRET` env var (required, >= 32 chars) |
| Access token TTL | 15 minutes |
| Refresh token TTL | 7 days |
| Refresh token rotation | On every refresh — new token issued, old revoked (F-4) |
| Refresh token family | `token_family UUID` — reuse detection revokes entire family (F-4) |
| Refresh token storage | `auth.refresh_token` table, token hashed with SHA-256 |

### 4.4 Required JWT claims (F-3)

Access tokens must contain and the API must validate:

| Claim | Value | Validation |
|---|---|---|
| `iss` | `"aslan-core"` | Exact match |
| `aud` | `"aslan-api"` | Exact match |
| `sub` | user_id (UUID string) | Must exist in auth.user, is_active=true |
| `exp` | Issued + 15min | Standard expiry check |
| `nbf` | Issue time | Not-before check |
| `iat` | Issue time | Present |
| `jti` | Random UUID | Present (for future revocation) |
| `typ` | `"access"` or `"refresh"` | Must be `"access"` for API endpoints |

Reject tokens with missing claims. Reject tokens where `sub` maps
to an inactive user.

### 4.5 Schema (migration 0033)

```sql
CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE auth.user (
    user_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user'
        CHECK (role IN ('user', 'admin')),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE auth.refresh_token (
    token_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL
        REFERENCES auth.user(user_id) ON DELETE CASCADE,
    token_family  UUID NOT NULL,
    token_hash    CHAR(64) NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX rt_user_id ON auth.refresh_token(user_id);
CREATE INDEX rt_family ON auth.refresh_token(token_family);
```

### 4.6 Refresh token rotation (F-4)

On `POST /auth/refresh`:
1. Hash the presented token, look up in `auth.refresh_token`
2. If not found or revoked → reject (401)
3. If found and not revoked:
   a. Check if this token was already used (revoked=true for this
      token_id). If so, **revoke all tokens in the family** (replay
      attack detected) → reject (401)
   b. Revoke the current token (set revoked=true)
   c. Issue new access + refresh token pair with the same
      `token_family`
   d. Store new refresh token hash

### 4.7 Rate limiting (F-9)

| Endpoint | Limit |
|---|---|
| `POST /auth/login` | 5 attempts per email per 15 minutes |
| `POST /auth/register` | 3 per IP per hour |
| `POST /auth/refresh` | 30 per user per hour |
| All other endpoints | 120 per user per minute |

Implemented via in-memory sliding window (single-process) or Redis
if multiple API workers are deployed. Return `429 Too Many Requests`
with `Retry-After` header.

### 4.8 Password requirements

- Minimum 10 characters
- No maximum (bcrypt truncates at 72 bytes — document this)
- No complexity rules (length is the primary defense)

### 4.9 Auth error responses (F-9)

All auth errors return the same shape to prevent user enumeration:

```json
{"detail": "Invalid credentials", "status_code": 401}
```

Do not distinguish between "user not found" and "wrong password" in
login responses.

## 5. Query layer (`aslan_core.query`)

Library functions that wrap SQL queries and return Pydantic models.

### 5.1 financials.py

```python
async def get_entity_financials(
    session: AsyncSession,
    caller: Principal,
    entity_id: UUID,
    *,
    statement_type: StatementType | None = None,
    period_type: PeriodType | None = None,
    restatement_basis: RestatementBasis = RestatementBasis.AS_REPORTED,
    consolidation: Consolidation = Consolidation.CONSOLIDATED,
    limit: int = 20,
) -> list[CanonicalFinancialRow]: ...
```

```python
async def compare_metric(
    session: AsyncSession,
    caller: Principal,
    entity_ids: list[UUID],
    canonical_code: str,
    *,
    restatement_basis: RestatementBasis = RestatementBasis.CPI_NORMALIZED,
    period_type: PeriodType = PeriodType.QUARTERLY,
    consolidation: Consolidation = Consolidation.CONSOLIDATED,
    limit: int = 8,
) -> dict[UUID, list[MetricPoint]]: ...
```

```python
async def get_metric_timeseries(
    session: AsyncSession,
    caller: Principal,
    entity_id: UUID,
    canonical_code: str,
    *,
    restatement_basis: RestatementBasis | None = None,
    period_type: PeriodType = PeriodType.QUARTERLY,
    consolidation: Consolidation = Consolidation.CONSOLIDATED,
    limit: int = 20,
) -> list[TimeseriesPoint]: ...
```

### 5.2 entity.py

```python
async def list_entities(
    session: AsyncSession,
    caller: Principal,
    *,
    search: str | None = None,
    has_financials: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[EntitySummary], int]: ...
```

```python
async def get_entity_quality(
    session: AsyncSession,
    caller: Principal,
    entity_id: UUID,
    *,
    restatement_basis: RestatementBasis = RestatementBasis.AS_REPORTED,
    limit: int = 10,
) -> list[QualityScorePublic]: ...
```

### 5.3 catalog.py

```python
async def list_canonical_lines(
    statement_type: StatementType | None = None,
) -> list[CanonicalLineInfo]: ...
```

### 5.4 Principal and authorization contract (C-2)

Every query function (except catalog) takes a `caller: Principal`:

```python
@dataclass(frozen=True)
class Principal:
    user_id: UUID
    role: str  # "user", "admin"
    is_active: bool
```

For v1, the principal is only used for audit logging (who queried
what). Future: filter results by principal's entity grants.

The AI service must construct a `Principal` representing its service
identity:
```python
AI_PRINCIPAL = Principal(
    user_id=UUID("00000000-0000-0000-0000-000000000000"),
    role="service",
    is_active=True,
)
```

This prevents accidental anonymous access — every query has an
attributed caller.

### 5.5 Input validation (F-5)

All query functions use **typed enums**, not raw strings:

```python
class StatementType(str, Enum):
    IS = "is"
    BS = "bs"
    CF = "cf"

class PeriodType(str, Enum):
    QUARTERLY = "q"
    HALF_YEAR = "h"
    ANNUAL = "y"
    YTD = "ytd"

class RestatementBasis(str, Enum):
    AS_REPORTED = "as_reported"
    CPI_NORMALIZED = "cpi_normalized"

class Consolidation(str, Enum):
    CONSOLIDATED = "consolidated"
    UNCONSOLIDATED = "unconsolidated"
```

`canonical_code` is validated against the loaded manifest at the API
layer. `search` is capped at 100 characters and stripped of SQL
wildcards before use.

All SQL uses **bind parameters** via SQLAlchemy `text()` with
`:param` syntax. No string interpolation.

### 5.6 Query bounds (F-6, F-7)

| Parameter | Min | Max | Default |
|---|---|---|---|
| `limit` | 1 | 100 | varies by endpoint |
| `offset` | 0 | 10000 | 0 |
| `entity_ids` (compare) | 1 | 10 | — |
| `search` length | — | 100 chars | — |

Values outside bounds → 400. Duplicate entity_ids are deduplicated
silently. All SQL queries have a statement timeout of 5 seconds.

## 6. API endpoints

### 6.1 Auth

| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/auth/register` | `{ email, password, invite_code? }` | `{ user_id, email }` |
| POST | `/auth/login` | `{ email, password }` | `{ access_token, refresh_token, token_type, expires_in }` |
| POST | `/auth/refresh` | `{ refresh_token }` | `{ access_token, refresh_token, token_type, expires_in }` |

### 6.2 Entities

| Method | Path | Query params | Response |
|---|---|---|---|
| GET | `/entities` | `search`, `has_financials`, `limit`, `offset` | `{ items: [EntitySummary], total, offset, limit }` |
| GET | `/entities/{id}/financials` | `statement_type`, `period_type`, `restatement_basis`, `consolidation`, `limit` | `{ entity_id, legal_name, periods: [PeriodData] }` |
| GET | `/entities/{id}/quality` | `restatement_basis`, `limit` | `{ entity_id, legal_name, scores: [QualityScorePublic] }` |

### 6.3 Financials

| Method | Path | Query params | Response |
|---|---|---|---|
| GET | `/financials/compare` | `entity_ids` (comma-sep, max 10), `canonical_code`, `restatement_basis`, `period_type`, `consolidation`, `limit` | `{ canonical_code, description, entities: { uuid: [MetricPoint] } }` |
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
    period_type: PeriodType
    consolidation: Consolidation
    restatement_basis: RestatementBasis
    lines: dict[str, Decimal | None]
    quality_score: int | None

class MetricPoint(BaseModel):
    period_end: date
    period_type: PeriodType
    value: Decimal | None
    restatement_basis: RestatementBasis

class TimeseriesPoint(BaseModel):
    period_end: date
    period_type: PeriodType
    as_reported: Decimal | None
    cpi_normalized: Decimal | None
    cpi_base_date: date | None

class QualityScorePublic(BaseModel):
    """Public quality score — internal diagnostics stripped (F-8)."""
    period_end: date
    period_type: PeriodType
    consolidation: Consolidation
    score: int = Field(ge=0, le=100)
    insufficient_data: bool
    checks: dict[str, QualityCheckPublic]

class QualityCheckPublic(BaseModel):
    """Allowlisted check output — no internal details."""
    state: str  # "passed", "failed", "skipped_missing_input", "informational"
    delta_pct: float | None = None
    coverage_pct: float | None = None

class CanonicalLineInfo(BaseModel):
    canonical_code: str
    statement_type: StatementType
    description: str
    computed: bool
    required: bool
    monetary: bool
```

Key changes from rev 1:
- `Decimal` instead of `float` for monetary values (F-11)
- Enum types instead of raw strings (F-11)
- `QualityScorePublic` with allowlisted check output, no raw JSONB (F-8)
- `Field(ge=0, le=100)` on score (F-11)

## 8. Error responses

All errors return JSON with consistent shape via custom exception
handlers (F-12):

```json
{
  "detail": "Entity not found",
  "status_code": 404
}
```

FastAPI's default 422 is overridden to match this shape with
sanitized field names (no internal model details).

| Status | When |
|---|---|
| 400 | Invalid query params (bad UUID, unknown canonical_code, out-of-bounds limit) |
| 401 | Missing/expired/invalid token, inactive user |
| 404 | Entity not found (entity_id does not exist) |
| 422 | Request body validation error |
| 429 | Rate limit exceeded |
| 500 | Internal server error (no stack trace in response) |

**Empty results are not 404 (F-10).** Valid entity + no data for
filters → 200 with empty `periods`, `points`, or `scores` array.

## 9. File structure

```
aslan-core/src/aslan_core/
  query/
    __init__.py
    financials.py      # get_entity_financials, compare_metric, get_metric_timeseries
    entity.py          # list_entities, get_entity_quality
    catalog.py         # list_canonical_lines
    schemas.py         # Pydantic models, enums, Principal
  api/
    __init__.py        # FastAPI app factory: create_api_app()
    auth.py            # register, login, refresh endpoints + JWT utils
    deps.py            # Depends() for session, current_user, rate_limiter
    middleware.py      # custom exception handlers, error sanitization
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
`ASLAN_JWT_SECRET` env var (>= 32 chars, fails startup if missing
or too short).

### 10.2 Docker Compose

New service in `infra/deploy/docker-compose.yml`:

```yaml
  api:
    build:
      context: ../..
      dockerfile: infra/deploy/Dockerfile
    environment:
      ASLAN_PG_DSN: "${ASLAN_PG_DSN}"
      ASLAN_JWT_SECRET: "${ASLAN_JWT_SECRET}"
    ports:
      - "127.0.0.1:8600:8600"
    entrypoint: ["aslan", "api", "serve", "--host", "0.0.0.0", "--port", "8600"]
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped
```

Credentials from env vars, not hardcoded (F-14).
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

Optional extra so base install isn't bloated.

## 12. Testing

### 12.1 Query layer unit tests

- `test_get_entity_financials` — seed canonical rows, verify
  filtering by statement_type, period_type, restatement_basis
- `test_compare_metric` — seed 2 entities, verify cross-entity
  response keyed by UUID, max 10 entity_ids enforced
- `test_get_metric_timeseries` — verify both-bases response when
  restatement_basis=None
- `test_list_entities` — search by name, pagination, bounds
- `test_get_entity_quality` — verify public check output (no raw JSONB)
- `test_enum_validation` — invalid statement_type/period_type rejected

### 12.2 API integration tests

Using FastAPI `TestClient`:
- Auth flow: register (with invite) → login → access endpoint → refresh
  (verify rotation: old token rejected, new token works)
- Expired token rejection (mock time)
- Invalid credentials → 401 (uniform message)
- Inactive user → 401 (even with valid token)
- Missing JWT claims → 401
- Each endpoint returns correct shape with enum types
- Query parameter validation (invalid UUID, unknown canonical_code,
  limit > 100, entity_ids > 10)
- Rate limit → 429
- Empty result → 200 with empty array (not 404)

### 12.3 Auth tests

- Password hashed with bcrypt (not stored plaintext)
- Refresh token hashed in DB
- Refresh token rotation: new pair issued, old revoked
- Refresh token reuse → entire family revoked
- Inactive user can't login
- Open registration blocked (no invite_code → 403)
- Password minimum length enforced

## 13. Migrations

| Migration | What |
|---|---|
| 0033 | `auth` schema + `auth.user` + `auth.refresh_token` tables |

## 14. Version target

aslan-core v0.10.0 (migration 0033 + query module + API service).

## Appendix: Codex adversarial review findings

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| C-1 | Critical | No authorization model — any registered user sees all data | §4.2: explicit model; data is public (KAP), auth gate is active user; registration gated by invite |
| C-2 | Critical | Query layer has no auth context — direct callers bypass JWT | §5.4: Principal required on all query functions |
| F-3 | High | JWT claims underspecified — no iss/aud/sub validation | §4.4: full claim table with exact validation rules |
| F-4 | High | No refresh token rotation — leaked tokens replayable 7d | §4.6: rotation on every refresh, family-based reuse detection |
| F-5 | High | SQL injection risk from free-form string params | §5.5: typed enums, bind params only, manifest validation |
| F-6 | High | No limit bounds — unbounded queries possible | §5.6: strict min/max/default for all params |
| F-7 | High | No max entity_ids in compare — huge IN queries | §5.6: max 10, deduplicated |
| F-8 | High | Raw JSONB quality checks exposed — leaks internals | §7: QualityScorePublic with allowlisted fields |
| F-9 | Medium | No rate limiting or lockout on auth endpoints | §4.7: per-endpoint rate limits |
| F-10 | Medium | 404 for empty results conflates missing entity with no data | §8: 200 with empty array for valid entity + no data |
| F-11 | Medium | float for money, unconstrained strings for domain values | §7: Decimal, enums, Field constraints |
| F-12 | Medium | FastAPI 422 doesn't match declared error shape | §8: custom exception handlers |
| F-13 | Medium | Entity enumeration via /entities search | §5.6: search capped at 100 chars; data is public KAP filings |
| F-14 | Low | Docker Compose hardcodes DB credentials | §10.2: env vars only |
