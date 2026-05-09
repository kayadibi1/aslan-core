"""Phase 4 — Bitemporal Research API HTTP endpoint tests.

Drives the FastAPI router from ``aslan_core.api.routes.research``
against the testcontainer Postgres via Starlette's synchronous
``TestClient``.

Cases (per docs/specs/bitemporal-research-api/TESTPLAN.md):

- TC-029 / TC-041 — naive datetime as_of returns 400 BITEMPORAL_AS_OF_NAIVE.
- TC-030 — future as_of returns 400 BITEMPORAL_AS_OF_FUTURE.
- TC-031 / TC-027 — missing X-Aslan-Api-Key returns 401 AUTH_INVALID.
- TC-032 / TC-028 — invalid API key returns 401 AUTH_INVALID.
- TC-033 / TC-073 — master flag off returns 503 FEATURE_DISABLED.
- TC-034 — /healthz returns 200 always.
- TC-042 — UTC ``Z`` suffix accepted on as_of.
- TC-056 — /verify/moat-2 returns 200 green when MOAT_2_CANARY_STATUS=true.
- TC-057 — /verify/moat-2 returns 503 red when MOAT_2_CANARY_STATUS=false.

Per SCOPE.md D5, D13, D16, D20, D27.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC

import psycopg
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from aslan_core.api.routes.research import router as research_router
from aslan_core.db.engine import create_engine as _create_engine

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


def _libpq_dsn(asyncpg_dsn: str) -> str:
    """Convert SQLAlchemy +asyncpg DSN to a psycopg-compatible libpq URL."""
    if asyncpg_dsn.startswith("postgresql+asyncpg://"):
        return "postgresql://" + asyncpg_dsn[len("postgresql+asyncpg://") :]
    return asyncpg_dsn


@pytest.fixture()
def app(pg_dsn: str) -> Iterator[FastAPI]:
    """FastAPI app with only the research router mounted.

    Per ``tests/integration/test_api.py``: a fresh engine per test
    avoids cross-event-loop pool pollution under Starlette TestClient.
    """

    @asynccontextmanager
    async def _lifespan(a: FastAPI) -> AsyncIterator[None]:
        a.state.engine = _create_engine(pg_dsn)
        try:
            yield
        finally:
            await a.state.engine.dispose()

    a = FastAPI(title="Aslan Research API (test)", version="test", lifespan=_lifespan)
    a.include_router(research_router)
    yield a


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def db_dsn(pg_dsn: str) -> str:
    """libpq DSN for synchronous psycopg flag manipulation."""
    return _libpq_dsn(pg_dsn)


def _set_master_flag(dsn: str, *, value: bool) -> None:
    """Toggle BITEMPORAL_API_ENABLED in aslan_core.feature_flags."""
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE aslan_core.feature_flags "
            "SET value_bool = %s, updated_at = now(), updated_by = 'tests' "
            "WHERE flag_name = 'BITEMPORAL_API_ENABLED'",
            (value,),
        )
        conn.commit()


def _set_canary_status(dsn: str, *, green: bool) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO aslan_core.feature_flags "
            "(flag_name, value_bool, scope, updated_at, updated_by) "
            "VALUES ('MOAT_2_CANARY_STATUS', %s, 'global', now(), 'tests') "
            "ON CONFLICT (flag_name) DO UPDATE "
            "SET value_bool = EXCLUDED.value_bool, "
            "    updated_at = EXCLUDED.updated_at, "
            "    updated_by = EXCLUDED.updated_by",
            (green,),
        )
        conn.commit()


def _clear_canary_status(dsn: str) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM aslan_core.feature_flags "
            "WHERE flag_name = 'MOAT_2_CANARY_STATUS'"
        )
        conn.commit()


# ---------------------------------------------------------------------
# Public endpoint smoke (no auth, no master flag)
# ---------------------------------------------------------------------


def test_healthz_always_returns_200(client: TestClient) -> None:
    """TC-034 — /healthz returns 200 regardless of feature flag state."""
    resp = client.get("/v1/research/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------
# Master flag gating + auth
# ---------------------------------------------------------------------


def test_master_flag_off_returns_503_on_observations(
    client: TestClient, db_dsn: str
) -> None:
    """TC-033 / TC-073 — master flag off → 503 FEATURE_DISABLED.

    Default seeded value of BITEMPORAL_API_ENABLED is ``false`` per
    migration 0048; the request must be refused before reaching auth.
    """
    _set_master_flag(db_dsn, value=False)

    resp = client.get("/v1/research/observations")
    assert resp.status_code == 503
    body = resp.json()
    detail = body.get("detail", body)
    assert detail.get("code") == "FEATURE_DISABLED"


def test_master_flag_on_then_missing_api_key_returns_401(
    client: TestClient, db_dsn: str
) -> None:
    """TC-031 / TC-027 — master on, no API key → 401 AUTH_INVALID."""
    _set_master_flag(db_dsn, value=True)
    try:
        resp = client.get("/v1/research/observations")
        assert resp.status_code == 401
        detail = resp.json().get("detail", resp.json())
        assert detail.get("code") == "AUTH_INVALID"
    finally:
        _set_master_flag(db_dsn, value=False)


def test_master_flag_on_with_invalid_api_key_returns_401(
    client: TestClient, db_dsn: str
) -> None:
    """TC-032 / TC-028 — invalid API key → 401 AUTH_INVALID.

    The header expects ``key_id:secret``; freeform garbage fails parsing
    and the request fails closed.
    """
    _set_master_flag(db_dsn, value=True)
    try:
        resp = client.get(
            "/v1/research/observations",
            headers={"X-Aslan-Api-Key": "aslan_test_random_garbage"},
        )
        assert resp.status_code == 401
        detail = resp.json().get("detail", resp.json())
        assert detail.get("code") == "AUTH_INVALID"
    finally:
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# Timezone correctness on as_of (TC-029 / TC-041, TC-030, TC-042)
#
# These exercise the parser directly (not via the HTTP route) because
# /observations requires auth — testing the parser unit-style avoids
# seeding API keys for what is fundamentally a string-parsing contract.
# ---------------------------------------------------------------------


def test_naive_as_of_rejected() -> None:
    """TC-029 / TC-041 — naive datetime → 400 BITEMPORAL_AS_OF_NAIVE."""
    from fastapi import HTTPException

    from aslan_core.api.routes.research import _parse_as_of

    with pytest.raises(HTTPException) as ei:
        _parse_as_of("2024-06-15T13:30:00")  # no Z, no offset
    assert ei.value.status_code == 400
    detail = ei.value.detail
    assert isinstance(detail, dict) and detail.get("code") == "BITEMPORAL_AS_OF_NAIVE"


def test_future_as_of_rejected() -> None:
    """TC-030 — far-future as_of → 400 BITEMPORAL_AS_OF_FUTURE."""
    from fastapi import HTTPException

    from aslan_core.api.routes.research import _parse_as_of

    with pytest.raises(HTTPException) as ei:
        _parse_as_of("2099-01-01T00:00:00Z")
    assert ei.value.status_code == 400
    detail = ei.value.detail
    assert isinstance(detail, dict) and detail.get("code") == "BITEMPORAL_AS_OF_FUTURE"


def test_utc_z_suffix_accepted() -> None:
    """TC-042 — ``Z`` suffix is the canonical accepted form."""

    from aslan_core.api.routes.research import _parse_as_of

    parsed = _parse_as_of("2024-06-15T13:30:00Z")
    assert parsed is not None
    assert parsed.tzinfo == UTC


# ---------------------------------------------------------------------
# /verify/moat-2 (TC-056 / TC-057)
# ---------------------------------------------------------------------


def test_verify_moat_2_green_when_canary_true(
    client: TestClient, db_dsn: str
) -> None:
    """TC-056 — canary green → /verify/moat-2 returns 200 with green shape."""
    _set_canary_status(db_dsn, green=True)
    try:
        resp = client.get("/v1/research/verify/moat-2")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("moat_2") == "green"
        assert "last_run_at" in body
    finally:
        _clear_canary_status(db_dsn)


def test_verify_moat_2_red_when_canary_false(
    client: TestClient, db_dsn: str
) -> None:
    """TC-057 — canary red → /verify/moat-2 returns 503 with red shape."""
    _set_canary_status(db_dsn, green=False)
    try:
        resp = client.get("/v1/research/verify/moat-2")
        assert resp.status_code == 503
        body = resp.json()
        assert body.get("moat_2") == "red"
        assert "last_run_at" in body
    finally:
        _clear_canary_status(db_dsn)
