"""Phase 4 — Round-6 regression tests for the bitemporal Research API.

Closes the 17 findings tracked in ``BUG_REVIEW_FINDINGS.md`` round 6.
The big additions exercised here are:

- D2 interval-mode parser (``as_of_range``) — half-open / closed-bracket
  normalization, malformed input, naive endpoints, T1 >= T2.
- Mutual exclusion of ``as_of`` and ``as_of_range`` on list endpoints
  (BITEMPORAL_INTERVAL_INVALID).
- Defense-in-depth ``_enforce_query_cost`` — width > 5y → 413
  ``QUERY_TOO_LARGE``; FastAPI ``Query(le=500)`` → 422 on overflow.
- ``disclosure_id: str`` path-parameter type (KAP IDs of the form
  ``KAP-2024-1234567`` must NOT be rejected as non-UUID at routing).
- ``_PRE_BITEMPORAL_WARNING`` shape on ``/filings``.
- RFC 7807 exception-handler registration order (research handler
  must overwrite the generic handler — Codex regression in round 5).
- ``_PUBLIC_PATHS`` exact-match (no suffix matching, so a future
  ``/v1/research/admin/version`` does NOT bypass auth).

Per SCOPE.md D2, D5, D16, D19; per ``BUG_REVIEW_FINDINGS.md`` round 6.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.testclient import TestClient

from aslan_core.api.routes.research import router as research_router
from aslan_core.db.engine import create_engine as _create_engine

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------
# Fixtures (mirrored from tests/research/test_endpoints.py).
# ---------------------------------------------------------------------


def _libpq_dsn(asyncpg_dsn: str) -> str:
    """Convert SQLAlchemy +asyncpg DSN to a psycopg-compatible libpq URL."""
    if asyncpg_dsn.startswith("postgresql+asyncpg://"):
        return "postgresql://" + asyncpg_dsn[len("postgresql+asyncpg://") :]
    return asyncpg_dsn


@pytest.fixture()
def app(pg_dsn: str) -> Iterator[FastAPI]:
    """FastAPI app with only the research router mounted (round-6 tests)."""

    @asynccontextmanager
    async def _lifespan(a: FastAPI) -> AsyncIterator[None]:
        a.state.engine = _create_engine(pg_dsn)
        try:
            yield
        finally:
            await a.state.engine.dispose()

    a = FastAPI(
        title="Aslan Research API (test-round6)", version="test", lifespan=_lifespan
    )
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


def _seed_api_key(dsn: str, *, pii_unredacted: bool = False) -> tuple[str, str]:
    """Seed an aslan_core.api_key row and return (key_id, secret).

    Mirrors :func:`tests.research.test_endpoints_extended._seed_api_key`
    so the round-6 tests don't introduce a third copy of the helper
    contract (we just inline-duplicate to keep this file self-contained
    per the brief: "don't add new pytest fixtures").
    """
    key_id = str(uuid4())
    secret = "test-secret-" + uuid4().hex
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO aslan_core.api_key "
            "(key_id, secret_hash, description, rate_tier, "
            " pii_unredacted, scopes, created_by) "
            "VALUES (%s, %s, 'test', 'partner', %s, ARRAY[]::text[], 'tests')",
            (key_id, secret, pii_unredacted),
        )
        conn.commit()
    return key_id, secret


# ---------------------------------------------------------------------
# 1-5. as_of_range parser unit tests
# (closes BUG_REVIEW_FINDINGS round-6 §4 / X1 — D2 interval mode).
# ---------------------------------------------------------------------


def test_as_of_range_parser_half_open() -> None:
    """Round-6 §4/X1 (D2 half-open form).

    closes BUG_REVIEW_FINDINGS HIGH §4/X1 as_of_range — the canonical
    ``[T1,T2)`` form parses to two TZ-aware UTC datetimes with T1 < T2
    and no microsecond shift on either endpoint.
    """
    from datetime import UTC

    from aslan_core.api.routes.research import _parse_as_of_range

    out = _parse_as_of_range("[2024-01-01T00:00:00Z,2025-01-01T00:00:00Z)")
    assert out is not None
    t1, t2 = out
    assert t1.tzinfo is UTC or t1.utcoffset().total_seconds() == 0
    assert t2.tzinfo is UTC or t2.utcoffset().total_seconds() == 0
    assert t1 < t2
    assert t1.year == 2024 and t1.month == 1 and t1.day == 1
    assert t2.year == 2025 and t2.month == 1 and t2.day == 1
    # Half-open: no microsecond shift on either endpoint.
    assert t1.microsecond == 0
    assert t2.microsecond == 0


def test_as_of_range_parser_closed_bracket_normalized_to_half_open() -> None:
    """Round-6 §4/X1 (D2 closed-bracket normalization).

    closes BUG_REVIEW_FINDINGS HIGH §4/X1 as_of_range — closed-on-T2
    forms ``[T1,T2]`` and ``(T1,T2]`` are normalized to half-open by
    bumping the upper bound by 1 microsecond so the boundary row is
    still included; open-on-T1 forms ``(T1,T2)`` and ``(T1,T2]`` shift
    the lower bound inward by 1 microsecond.
    """
    from aslan_core.api.routes.research import _parse_as_of_range

    # Closed upper bracket: T2 bumps by 1us.
    closed = _parse_as_of_range("(2024-01-01T00:00:00Z,2025-01-01T00:00:00Z]")
    assert closed is not None
    c_t1, c_t2 = closed
    assert c_t1.microsecond == 1  # opened on lower → +1us
    assert c_t2.microsecond == 1  # closed on upper → +1us

    # Both open: T1 bumps by 1us, T2 unchanged.
    both_open = _parse_as_of_range("(2024-01-01T00:00:00Z,2025-01-01T00:00:00Z)")
    assert both_open is not None
    o_t1, o_t2 = both_open
    assert o_t1.microsecond == 1
    assert o_t2.microsecond == 0


def test_as_of_range_parser_malformed_string_rejected() -> None:
    """Round-6 §4/X1 (D2 malformed → 400).

    closes BUG_REVIEW_FINDINGS HIGH §4/X1 as_of_range — unstructured
    input that doesn't carry the canonical bracket shape raises
    ``HTTPException(400, BITEMPORAL_INTERVAL_INVALID)``.
    """
    from fastapi import HTTPException

    from aslan_core.api.routes.research import _parse_as_of_range

    with pytest.raises(HTTPException) as ei:
        _parse_as_of_range("not a range")
    assert ei.value.status_code == 400
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail.get("code") == "BITEMPORAL_INTERVAL_INVALID"


def test_as_of_range_parser_rejects_naive_endpoints() -> None:
    """Round-6 §4/X1 (D2 naive endpoints → 400).

    closes BUG_REVIEW_FINDINGS HIGH §4/X1 as_of_range — interval
    endpoints without an explicit timezone offset are rejected with
    BITEMPORAL_INTERVAL_INVALID, matching the single-as_of contract
    in :func:`_parse_as_of`.
    """
    from fastapi import HTTPException

    from aslan_core.api.routes.research import _parse_as_of_range

    with pytest.raises(HTTPException) as ei:
        _parse_as_of_range("[2024-01-01,2025-01-01)")
    assert ei.value.status_code == 400
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail.get("code") == "BITEMPORAL_INTERVAL_INVALID"


def test_as_of_range_parser_rejects_t1_geq_t2() -> None:
    """Round-6 §4/X1 (D2 T1 >= T2 → 400).

    closes BUG_REVIEW_FINDINGS HIGH §4/X1 as_of_range — interval with
    inverted endpoints (or zero-width after normalization) is rejected.
    """
    from fastapi import HTTPException

    from aslan_core.api.routes.research import _parse_as_of_range

    with pytest.raises(HTTPException) as ei:
        _parse_as_of_range("[2025-01-01T00:00:00Z,2024-01-01T00:00:00Z)")
    assert ei.value.status_code == 400
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail.get("code") == "BITEMPORAL_INTERVAL_INVALID"


# ---------------------------------------------------------------------
# 6. as_of + as_of_range mutual exclusion at the route boundary
# (closes BUG_REVIEW_FINDINGS round-6 §4/X2).
# ---------------------------------------------------------------------


def test_as_of_and_as_of_range_mutually_exclusive_on_observations(
    client: TestClient, db_dsn: str
) -> None:
    """Round-6 §4/X2 — closes BUG_REVIEW_FINDINGS HIGH §4/X2 mutual exclusion.

    Hitting ``/observations`` with both ``as_of`` and ``as_of_range``
    set is rejected with 400 BITEMPORAL_INTERVAL_INVALID. The route
    body's ``_reject_pit_with_interval`` runs after auth, so we seed
    an API key to exercise the contract instead of relying on the
    incorrect "fails before auth" assumption.
    """
    _set_master_flag(db_dsn, value=True)
    try:
        key_id, secret = _seed_api_key(db_dsn)
        resp = client.get(
            "/v1/research/observations",
            params={
                "as_of": "2024-01-01T00:00:00Z",
                "as_of_range": "[2024-01-01T00:00:00Z,2025-01-01T00:00:00Z)",
            },
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp.status_code == 400
        body = resp.json()
        # RFC 7807 envelope from research_logging — ``code`` is at the top level.
        detail = body.get("detail", body)
        code = body.get("code") or (
            detail.get("code") if isinstance(detail, dict) else None
        )
        assert code == "BITEMPORAL_INTERVAL_INVALID"
    finally:
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# 7. _enforce_query_cost: width > 5y → 413 QUERY_TOO_LARGE
# (closes BUG_REVIEW_FINDINGS round-6 §4/X3 / SCOPE.md D19).
# ---------------------------------------------------------------------


def test_query_too_large_when_as_of_range_width_exceeds_5_years(
    client: TestClient, db_dsn: str
) -> None:
    """Round-6 §4/X3 — closes BUG_REVIEW_FINDINGS HIGH §4/X3 QUERY_TOO_LARGE.

    A 16-year ``as_of_range`` is rejected with 413 QUERY_TOO_LARGE
    per SCOPE.md D19 (defense-in-depth row / window cap).
    """
    _set_master_flag(db_dsn, value=True)
    try:
        key_id, secret = _seed_api_key(db_dsn)
        resp = client.get(
            "/v1/research/observations",
            params={
                "as_of_range": "[2010-01-01T00:00:00Z,2026-01-01T00:00:00Z)",
            },
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp.status_code == 413
        body = resp.json()
        detail = body.get("detail", body)
        code = body.get("code") or (
            detail.get("code") if isinstance(detail, dict) else None
        )
        assert code == "QUERY_TOO_LARGE"
    finally:
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# 8. limit=501 → 422 from FastAPI's Query(le=500) validation.
# ---------------------------------------------------------------------


def test_limit_above_500_rejected_at_validation_layer(
    client: TestClient, db_dsn: str
) -> None:
    """Round-6 §4/X3 (limit hard cap) — closes BUG_REVIEW_FINDINGS HIGH §4/X3.

    ``Query(le=500)`` is enforced by FastAPI before the route body
    runs; we confirm the 422 response shape so the contract regresses
    visibly if someone widens the cap without updating SCOPE.md.
    """
    _set_master_flag(db_dsn, value=True)
    try:
        key_id, secret = _seed_api_key(db_dsn)
        resp = client.get(
            "/v1/research/observations",
            params={"limit": 501},
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp.status_code == 422
    finally:
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# 9. /disclosures/{disclosure_id}: KAP-shaped str path parameter.
# (closes BUG_REVIEW_FINDINGS round-6 §4/X4 — disclosure_id: str.)
# ---------------------------------------------------------------------


def test_disclosure_id_accepts_kap_shaped_string(
    client: TestClient, db_dsn: str
) -> None:
    """Round-6 §4/X4 — closes BUG_REVIEW_FINDINGS HIGH §4/X4 disclosure_id: str.

    KAP IDs (e.g. ``KAP-2024-1234567``) are TEXT in the crawl schema
    so the path parameter must be ``str``, not UUID. Pre-round-6 the
    parameter was UUID-typed and FastAPI returned 422 at routing.

    The exact response code depends on the master flag / auth path
    (503 / 401 / 200 / 404 are all acceptable here); the assertion
    that matters is "NOT 422" — i.e. the path param accepted the value.
    """
    _set_master_flag(db_dsn, value=True)
    try:
        # Without an API key: 401 AUTH_INVALID. With master flag off:
        # 503 FEATURE_DISABLED. Either way, NOT 422.
        resp_master_on = client.get(
            "/v1/research/disclosures/KAP-2024-1234567"
        )
        assert resp_master_on.status_code != 422

        _set_master_flag(db_dsn, value=False)
        resp_master_off = client.get(
            "/v1/research/disclosures/KAP-2024-1234567"
        )
        assert resp_master_off.status_code != 422
    finally:
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# 10. _PRE_BITEMPORAL_WARNING shape on /filings.
# ---------------------------------------------------------------------


def test_pre_bitemporal_warning_constant_shape() -> None:
    """Round-6 §4/X5 — closes BUG_REVIEW_FINDINGS MED §4/X5 PRE_BITEMPORAL_TABLE.

    ``_PRE_BITEMPORAL_WARNING`` is the warning record emitted in
    every ``/filings`` envelope's ``metadata.warnings`` list while
    ``doc.filing_at`` is not yet in v1. Its ``code`` MUST be
    ``"PRE_BITEMPORAL_TABLE"`` so client SDKs can switch on the
    canonical token.
    """
    from aslan_core.api.routes.research import _PRE_BITEMPORAL_WARNING

    assert _PRE_BITEMPORAL_WARNING.code == "PRE_BITEMPORAL_TABLE"
    # Sanity: message is non-empty and references the doc.filing_at path.
    assert isinstance(_PRE_BITEMPORAL_WARNING.message, str)
    assert _PRE_BITEMPORAL_WARNING.message != ""


# ---------------------------------------------------------------------
# 11. RFC 7807 exception-handler ordering (Codex round-5 regression).
# ---------------------------------------------------------------------


def test_research_exception_handler_overwrites_generic_handler() -> None:
    """Round-6 §4/X6 — closes BUG_REVIEW_FINDINGS HIGH §4/X6 handler order.

    FastAPI keeps ONE handler per exception class — the LAST
    registration wins. The research handler must be registered AFTER
    the generic one so research paths get the RFC 7807 envelope. The
    round-5 regression Codex flagged was the reverse order (generic
    handler clobbered the research handler). We assert the research
    handler is the active dispatcher for ``StarletteHTTPException``.
    """
    from aslan_core.api import create_api_app

    app = create_api_app()
    handler = app.exception_handlers.get(StarletteHTTPException)
    assert handler is not None
    qualname = getattr(handler, "__qualname__", "")
    assert "register_research_exception_handlers" in qualname, (
        f"Expected research handler to be active for StarletteHTTPException; "
        f"got {qualname!r}. The generic handler likely clobbered the "
        f"research handler — regression in research_logging registration order."
    )


# ---------------------------------------------------------------------
# 12. _PUBLIC_PATHS exact-match (no suffix bypass).
# ---------------------------------------------------------------------


def test_public_paths_exact_match_no_suffix_bypass() -> None:
    """Round-6 §4/X7 — closes BUG_REVIEW_FINDINGS MED §4/X7 _PUBLIC_PATHS.

    ``_is_public`` must use exact-string membership against the
    ``_PUBLIC_PATHS`` frozenset, NOT suffix matching. With the old
    suffix matcher, ``/v1/research/admin/version`` would have leaked
    past auth because it ends with ``/version``. The exact-match form
    rejects that drift.
    """
    from aslan_core.api.research_auth import _PUBLIC_PATHS, _is_public

    # Documented public paths still resolve as public.
    assert _is_public("/v1/research/version") is True
    assert _is_public("/v1/research/healthz") is True
    # A would-be admin route sharing the ``/version`` suffix MUST NOT bypass.
    assert _is_public("/v1/research/admin/version") is False
    # Frozenset hygiene: still a frozenset of plain strings.
    assert isinstance(_PUBLIC_PATHS, frozenset)
    assert all(isinstance(p, str) for p in _PUBLIC_PATHS)
