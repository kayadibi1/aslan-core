"""Integration tests for the Aslan Financial API (Tasks 9-14).

Uses the real Postgres testcontainer (via shared fixtures) so auth
queries hit the actual ``auth`` schema tables.  Query-layer endpoints
are tested against the seeded timeseries tables from the migration stack.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aslan_core.db.session import create_session_factory
from aslan_core.query.schemas import CanonicalLineInfo, StatementType

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

_SECRET = "a" * 64
_INVITE_CODE = "test-invite-2026"


@pytest.fixture(autouse=True)
def _api_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set required env vars for auth + invite."""
    monkeypatch.setenv("ASLAN_JWT_SECRET", _SECRET)
    monkeypatch.setenv("ASLAN_INVITE_CODE", _INVITE_CODE)


# ---------------------------------------------------------------------------
# App + client fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(pg_dsn: str) -> Iterator[Any]:
    """Build the FastAPI app with an isolated engine for the TestClient.

    Starlette's synchronous ``TestClient`` spins up its own event loop
    internally.  If we reuse the session-scoped ``engine`` fixture here,
    asyncpg connections get checked out on the TestClient's loop but
    returned to a pool whose other consumers (``_wipe_streams_tables_after``,
    ``session``, etc.) run on pytest-asyncio's session loop, causing
    ``RuntimeError: Task got Future attached to a different loop``.

    Creating a dedicated engine per test avoids cross-loop pool pollution.
    The engine is disposed synchronously inside the TestClient's own loop
    via a FastAPI shutdown event, so no connections leak.
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from aslan_core.api.middleware import register_exception_handlers
    from aslan_core.api.routes.auth import router as auth_router
    from aslan_core.api.routes.catalog import router as catalog_router
    from aslan_core.api.routes.entities import router as entities_router
    from aslan_core.api.routes.financials import router as financials_router
    from aslan_core.db.engine import create_engine as _create_engine

    @asynccontextmanager
    async def _lifespan(a: FastAPI) -> AsyncIterator[None]:
        a.state.engine = _create_engine(pg_dsn)
        try:
            yield
        finally:
            await a.state.engine.dispose()

    app = FastAPI(title="Aslan Financial API", version="0.10.0", lifespan=_lifespan)
    app.include_router(auth_router, prefix="/auth", tags=["auth"])
    app.include_router(entities_router, prefix="/entities", tags=["entities"])
    app.include_router(financials_router, prefix="/financials", tags=["financials"])
    app.include_router(catalog_router, tags=["catalog"])
    register_exception_handlers(app)

    # Inject catalog (read-only, no loop affinity).
    app.state.canonical_lines = [
        CanonicalLineInfo(
            canonical_code="revenue",
            statement_type=StatementType.IS,
            description="Total revenue",
            computed=False,
            required=True,
            monetary=True,
        ),
        CanonicalLineInfo(
            canonical_code="net_income",
            statement_type=StatementType.IS,
            description="Net income",
            computed=False,
            required=True,
            monetary=True,
        ),
    ]
    app.state.canonical_codes = {li.canonical_code for li in app.state.canonical_lines}
    yield app


@pytest.fixture()
def client(app: Any) -> Iterator[Any]:
    """Synchronous TestClient for the test FastAPI app."""
    from starlette.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# DB helper: seed test data
# ---------------------------------------------------------------------------


_API_SOURCE_ID = "test_api_seed"
_API_RUN_ID = 99_999
_PHASH = "a" * 64
_MHASH = "b" * 64


@pytest_asyncio.fixture(loop_scope="session")
async def _seed_entity(
    engine: AsyncEngine,
) -> AsyncIterator[UUID]:
    """Insert a test entity + some canonical financial rows for query tests.

    Creates the full FK dependency chain (source -> ingestion_run ->
    currency -> entity -> canonical_financial / entity_quality_score)
    so all NOT NULL columns are satisfied.
    """
    factory = create_session_factory(engine)
    entity_id = uuid4()

    async with factory() as session:
        # FK dependencies
        await session.execute(
            text(
                "INSERT INTO src.source (source_id, name, kind, license_status) "
                "VALUES (:sid, 'TestAPI', 'scraper', 'open') ON CONFLICT DO NOTHING"
            ),
            {"sid": _API_SOURCE_ID},
        )
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run "
                "(ingestion_run_id, source_id, job_name, status) "
                "VALUES (:run, :sid, 'seed', 'succeeded') ON CONFLICT DO NOTHING"
            ),
            {"run": _API_RUN_ID, "sid": _API_SOURCE_ID},
        )
        await session.execute(
            text(
                "INSERT INTO ref.currency (currency_code, name) "
                "VALUES ('TRY', 'Turkish Lira') ON CONFLICT DO NOTHING"
            )
        )

        # Entity
        await session.execute(
            text(
                "INSERT INTO ref.entity "
                "(entity_id, entity_type, legal_name, status, "
                " source_id, ingestion_run_id) "
                "VALUES (:eid, 'company', :name, 'active', :sid, :run)"
            ),
            {
                "eid": entity_id,
                "name": "Test Corp API",
                "sid": _API_SOURCE_ID,
                "run": _API_RUN_ID,
            },
        )
        # Canonical financial rows
        for i, code in enumerate(["revenue", "net_income"]):
            await session.execute(
                text(
                    "INSERT INTO ts.canonical_financial "
                    "(entity_id, canonical_code, period_end, period_type, "
                    " consolidation, restatement_basis, currency_code, "
                    " accounting_standard, payload_hash, source_contributions, "
                    " mapping_version, manifest_hash, as_of, "
                    " ingestion_run_id, value) "
                    "VALUES (:eid, :code, :pe, :pt, :con, :rb, 'TRY', "
                    " 'ifrs', :phash, :sc, :mv, :mhash, now(), :run, :val)"
                ),
                {
                    "eid": entity_id,
                    "code": code,
                    "pe": date(2025, 12, 31),
                    "pt": "q",
                    "con": "consolidated",
                    "rb": "as_reported",
                    "phash": _PHASH,
                    "sc": "{}",
                    "mv": 1,
                    "mhash": _MHASH,
                    "run": _API_RUN_ID,
                    "val": Decimal("1000000") + i * Decimal("500000"),
                },
            )
        # Quality score
        await session.execute(
            text(
                "INSERT INTO ts.entity_quality_score "
                "(entity_id, period_end, period_type, consolidation, "
                " currency_code, accounting_standard, restatement_basis, "
                " mapping_version, manifest_hash, as_of, "
                " ingestion_run_id, score, insufficient_data, checks) "
                "VALUES (:eid, :pe, :pt, :con, 'TRY', 'ifrs', :rb, "
                " :mv, :mhash, now(), :run, :sc, :isd, :ch)"
            ),
            {
                "eid": entity_id,
                "pe": date(2025, 12, 31),
                "pt": "q",
                "con": "consolidated",
                "rb": "as_reported",
                "mv": 1,
                "mhash": _MHASH,
                "run": _API_RUN_ID,
                "sc": 85,
                "isd": False,
                "ch": '{"completeness": {"state": "pass", "coverage_pct": 0.95}}',
            },
        )
        await session.commit()

    yield entity_id

    # Cleanup (reverse FK order)
    async with factory() as session:
        await session.execute(
            text("DELETE FROM ts.entity_quality_score WHERE entity_id = :eid"),
            {"eid": entity_id},
        )
        await session.execute(
            text("DELETE FROM ts.canonical_financial WHERE entity_id = :eid"),
            {"eid": entity_id},
        )
        await session.execute(
            text("DELETE FROM ref.entity WHERE entity_id = :eid"),
            {"eid": entity_id},
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register(client: Any, email: str | None = None) -> dict[str, Any]:
    """Register a user and return the response JSON."""
    email = email or f"test-{uuid4().hex[:8]}@example.com"
    resp = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "strong-pass-12345",
            "invite_code": _INVITE_CODE,
        },
    )
    return {"response": resp, "email": email}


def _login(client: Any, email: str, password: str = "strong-pass-12345") -> Any:
    return client.post("/auth/login", json={"email": email, "password": password})


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Auth: Register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_with_invite_code_200(self, client: Any) -> None:
        result = _register(client)
        resp = result["response"]
        assert resp.status_code == 200
        body = resp.json()
        assert "user_id" in body
        assert body["email"] == result["email"]

    def test_register_without_invite_403(self, client: Any) -> None:
        resp = client.post(
            "/auth/register",
            json={
                "email": "nobody@example.com",
                "password": "strong-pass-12345",
                "invite_code": "wrong-code",
            },
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Invalid invite code"

    def test_register_duplicate_email_400(self, client: Any) -> None:
        email = f"dup-{uuid4().hex[:8]}@example.com"
        r1 = _register(client, email)
        assert r1["response"].status_code == 200

        resp = client.post(
            "/auth/register",
            json={
                "email": email,
                "password": "another-pass-12345",
                "invite_code": _INVITE_CODE,
            },
        )
        assert resp.status_code == 400
        assert "already registered" in resp.json()["detail"]

    def test_register_short_password_422(self, client: Any) -> None:
        resp = client.post(
            "/auth/register",
            json={
                "email": "short@example.com",
                "password": "short",
                "invite_code": _INVITE_CODE,
            },
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Auth: Login
# ---------------------------------------------------------------------------


class TestLogin:
    def test_login_returns_tokens(self, client: Any) -> None:
        result = _register(client)
        resp = _login(client, result["email"])
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "bearer"

    def test_login_bad_password_401(self, client: Any) -> None:
        result = _register(client)
        resp = _login(client, result["email"], password="wrong-password-1234")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid credentials"

    def test_login_nonexistent_email_401(self, client: Any) -> None:
        resp = _login(client, "nonexistent@example.com")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid credentials"


# ---------------------------------------------------------------------------
# Auth: Refresh
# ---------------------------------------------------------------------------


class TestRefresh:
    def test_refresh_issues_new_tokens(self, client: Any) -> None:
        result = _register(client)
        login_resp = _login(client, result["email"])
        tokens = login_resp.json()

        resp = client.post(
            "/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]},
        )
        assert resp.status_code == 200
        new_tokens = resp.json()
        assert "access_token" in new_tokens
        assert "refresh_token" in new_tokens
        # New refresh token should be different
        assert new_tokens["refresh_token"] != tokens["refresh_token"]

    def test_refresh_old_token_rejected(self, client: Any) -> None:
        result = _register(client)
        login_resp = _login(client, result["email"])
        old_refresh = login_resp.json()["refresh_token"]

        # Use the token once (valid rotation)
        resp1 = client.post("/auth/refresh", json={"refresh_token": old_refresh})
        assert resp1.status_code == 200

        # Re-use the old token (reuse detection)
        resp2 = client.post("/auth/refresh", json={"refresh_token": old_refresh})
        assert resp2.status_code == 401

    def test_refresh_invalid_token_401(self, client: Any) -> None:
        resp = client.post(
            "/auth/refresh",
            json={"refresh_token": "completely-bogus-token"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Protected endpoints: auth required
# ---------------------------------------------------------------------------


class TestProtectedEndpoints:
    def test_access_with_token_200(self, client: Any) -> None:
        result = _register(client)
        tokens = _login(client, result["email"]).json()
        resp = client.get("/entities", headers=_auth_header(tokens["access_token"]))
        assert resp.status_code == 200

    def test_access_without_token_401(self, client: Any) -> None:
        resp = client.get("/entities")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Entity endpoints
# ---------------------------------------------------------------------------


class TestEntities:
    def test_entity_list_correct_shape(self, client: Any, _seed_entity: UUID) -> None:
        result = _register(client)
        tokens = _login(client, result["email"]).json()
        resp = client.get("/entities", headers=_auth_header(tokens["access_token"]))
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert isinstance(body["items"], list)

    def test_entity_list_empty_result_200(self, client: Any) -> None:
        result = _register(client)
        tokens = _login(client, result["email"]).json()
        resp = client.get(
            "/entities",
            params={"search": "zzz_nonexistent_entity_zzz"},
            headers=_auth_header(tokens["access_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []

    def test_limit_gt_100_returns_422(self, client: Any) -> None:
        result = _register(client)
        tokens = _login(client, result["email"]).json()
        resp = client.get(
            "/entities",
            params={"limit": 200},
            headers=_auth_header(tokens["access_token"]),
        )
        assert resp.status_code == 422

    def test_invalid_uuid_returns_400(self, client: Any) -> None:
        result = _register(client)
        tokens = _login(client, result["email"]).json()
        resp = client.get(
            "/entities/not-a-uuid/financials",
            headers=_auth_header(tokens["access_token"]),
        )
        assert resp.status_code == 400
        assert "Invalid UUID" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Financial endpoints
# ---------------------------------------------------------------------------


class TestFinancials:
    def test_entity_financials_returns_periods(self, client: Any, _seed_entity: UUID) -> None:
        result = _register(client)
        tokens = _login(client, result["email"]).json()
        resp = client.get(
            f"/entities/{_seed_entity}/financials",
            headers=_auth_header(tokens["access_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        if body:
            period = body[0]
            assert "period_end" in period
            assert "lines" in period
            # Values should be representable (string Decimals from JSON)
            for _code, val in period["lines"].items():
                if val is not None:
                    Decimal(str(val))

    def test_compare_returns_keyed_by_entity(self, client: Any, _seed_entity: UUID) -> None:
        result = _register(client)
        tokens = _login(client, result["email"]).json()
        resp = client.get(
            "/financials/compare",
            params={
                "entity_ids": str(_seed_entity),
                "canonical_code": "revenue",
            },
            headers=_auth_header(tokens["access_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, dict)
        # Key should be the entity UUID (as string)
        assert str(_seed_entity) in body

    def test_timeseries_returns_both_bases(self, client: Any, _seed_entity: UUID) -> None:
        result = _register(client)
        tokens = _login(client, result["email"]).json()
        resp = client.get(
            "/financials/timeseries",
            params={
                "entity_id": str(_seed_entity),
                "canonical_code": "revenue",
            },
            headers=_auth_header(tokens["access_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        if body:
            point = body[0]
            assert "as_reported" in point
            assert "cpi_normalized" in point

    def test_unknown_canonical_code_400(self, client: Any) -> None:
        result = _register(client)
        tokens = _login(client, result["email"]).json()
        eid = str(uuid4())
        resp = client.get(
            "/financials/compare",
            params={
                "entity_ids": eid,
                "canonical_code": "does_not_exist",
            },
            headers=_auth_header(tokens["access_token"]),
        )
        assert resp.status_code == 400
        assert "Unknown canonical_code" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Quality endpoint
# ---------------------------------------------------------------------------


class TestQuality:
    def test_quality_returns_public_checks(self, client: Any, _seed_entity: UUID) -> None:
        result = _register(client)
        tokens = _login(client, result["email"]).json()
        resp = client.get(
            f"/entities/{_seed_entity}/quality",
            headers=_auth_header(tokens["access_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        if body:
            score = body[0]
            assert "score" in score
            assert "checks" in score
            for _name, check in score["checks"].items():
                # Only public fields should be present
                allowed = {"state", "delta_pct", "coverage_pct"}
                assert set(check.keys()) <= allowed


# ---------------------------------------------------------------------------
# Catalog endpoint (no auth)
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_catalog_no_auth_needed(self, client: Any) -> None:
        resp = client.get("/canonical-lines")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        # Our fixture loaded two canonical lines
        assert len(body) == 2

    def test_catalog_filter_by_statement_type(self, client: Any) -> None:
        resp = client.get("/canonical-lines", params={"statement_type": "is"})
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        for item in body:
            assert item["statement_type"] == "is"
