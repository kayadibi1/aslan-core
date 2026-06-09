"""Phase 4 — Extended Bitemporal Research API endpoint tests (round 2).

Covers the eight endpoints implemented in research.py round 2:

- ``GET /v1/research/financials/line-items``
- ``GET /v1/research/entities``
- ``GET /v1/research/entities/{entity_id}``
- ``GET /v1/research/disclosures``
- ``GET /v1/research/disclosures/{disclosure_id}``
- ``GET /v1/research/filings``
- ``GET /v1/research/events``
- ``GET /v1/research/quality-scores``

Test cases (per ``docs/specs/bitemporal-research-api/TESTPLAN.md``):

- TC-029 / TC-041 — naive datetime as_of returns 400 BITEMPORAL_AS_OF_NAIVE
  (exercised on /financials/line-items, /disclosures, /events).
- TC-033 / TC-073 — master flag off → 503 FEATURE_DISABLED
  (exercised on /entities and /quality-scores).
- TC-027 / TC-031 — missing X-Aslan-Api-Key → 401 AUTH_INVALID
  (exercised on /filings and /financials/line-items).
- TC-003 / single-resource 404 — ``/entities/{nonexistent}`` and
  ``/disclosures/{nonexistent}``.
- TC-058 / TC-061 — PII redaction on /events: counterparty_name is
  replaced with ``<REDACTED:counterparty>`` token unless the
  principal carries ``pii_unredacted=true`` (D30).
- TC-008 — pre-bitemporal warning on /disclosures envelope
  (PRE_BITEMPORAL_TABLE).
- TC-001 / TC-073 — quality-scores envelope shape (data list +
  metadata.as_of_resolved) without seeding (empty result still has a
  well-formed envelope).

Per SCOPE.md D5, D13, D15, D16, D20, D27, D30.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from aslan_core.api.routes.research import router as research_router
from aslan_core.db.engine import create_engine as _create_engine

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------
# Fixtures (mirrored from tests/research/test_endpoints.py)
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

    a = FastAPI(title="Aslan Research API (test-extended)", version="test", lifespan=_lifespan)
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


# ---------------------------------------------------------------------
# 1. Naive as_of rejection (TC-029 / TC-041)
#
# Mirroring test_endpoints.py, we exercise the parser directly because
# the parsing contract is what we are validating; routing the request
# through HTTP would require seeding an API key for what is effectively
# a string-parser unit test. This keeps the assertion identical to the
# HTTP-layer contract while remaining deterministic.
# ---------------------------------------------------------------------


def test_naive_as_of_rejected_for_financials_line_items() -> None:
    """TC-029 / TC-041 — /financials/line-items naive as_of → 400."""
    from fastapi import HTTPException

    from aslan_core.api.routes.research import _parse_as_of

    with pytest.raises(HTTPException) as ei:
        _parse_as_of("2024-01-01T00:00:00")  # no Z, no offset
    assert ei.value.status_code == 400
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail.get("code") == "BITEMPORAL_AS_OF_NAIVE"


def test_naive_as_of_rejected_for_disclosures() -> None:
    """TC-029 / TC-041 — /disclosures naive as_of → 400."""
    from fastapi import HTTPException

    from aslan_core.api.routes.research import _parse_as_of

    with pytest.raises(HTTPException) as ei:
        _parse_as_of("2023-12-31T23:59:59")
    assert ei.value.status_code == 400
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail.get("code") == "BITEMPORAL_AS_OF_NAIVE"


def test_naive_as_of_rejected_for_events() -> None:
    """TC-029 / TC-041 — /events naive as_of → 400."""
    from fastapi import HTTPException

    from aslan_core.api.routes.research import _parse_as_of

    with pytest.raises(HTTPException) as ei:
        _parse_as_of("2025-06-15T13:30:00")
    assert ei.value.status_code == 400
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail.get("code") == "BITEMPORAL_AS_OF_NAIVE"


# ---------------------------------------------------------------------
# 2. Master flag off → 503 FEATURE_DISABLED (TC-033 / TC-073)
# ---------------------------------------------------------------------


def test_master_flag_off_returns_503_on_entities(client: TestClient, db_dsn: str) -> None:
    """TC-033 / TC-073 — master flag off → 503 on /entities."""
    _set_master_flag(db_dsn, value=False)

    resp = client.get("/v1/research/entities")
    assert resp.status_code == 503
    body = resp.json()
    detail = body.get("detail", body)
    assert detail.get("code") == "FEATURE_DISABLED"


def test_master_flag_off_returns_503_on_quality_scores(client: TestClient, db_dsn: str) -> None:
    """TC-033 / TC-073 — master flag off → 503 on /quality-scores."""
    _set_master_flag(db_dsn, value=False)

    resp = client.get("/v1/research/quality-scores")
    assert resp.status_code == 503
    body = resp.json()
    detail = body.get("detail", body)
    assert detail.get("code") == "FEATURE_DISABLED"


# ---------------------------------------------------------------------
# 3. Missing API key → 401 AUTH_INVALID (TC-027 / TC-031)
# ---------------------------------------------------------------------


def test_missing_api_key_returns_401_on_filings(client: TestClient, db_dsn: str) -> None:
    """TC-027 / TC-031 — master on, no API key → 401 on /filings."""
    _set_master_flag(db_dsn, value=True)
    try:
        resp = client.get("/v1/research/filings")
        assert resp.status_code == 401
        detail = resp.json().get("detail", resp.json())
        assert detail.get("code") == "AUTH_INVALID"
    finally:
        _set_master_flag(db_dsn, value=False)


def test_missing_api_key_returns_401_on_financials_line_items(
    client: TestClient, db_dsn: str
) -> None:
    """TC-027 / TC-031 — master on, no API key → 401 on /financials/line-items."""
    _set_master_flag(db_dsn, value=True)
    try:
        resp = client.get("/v1/research/financials/line-items")
        assert resp.status_code == 401
        detail = resp.json().get("detail", resp.json())
        assert detail.get("code") == "AUTH_INVALID"
    finally:
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# 4. Single-resource 404 — /entities/{id}, /disclosures/{id} (TC-003)
#
# We seed an API key and look up a UUID we know does not exist. The
# 404 path is the route's pre-bitemporal "not found at as_of" branch.
# ---------------------------------------------------------------------


def _seed_api_key(dsn: str, *, pii_unredacted: bool = False) -> tuple[str, str]:
    """Seed an aslan_core.api_key row and return (key_id, secret).

    Uses the legacy plaintext-equality fallback in research_auth.verify_secret
    so we do not need to depend on argon2 hashing in the test fixture.
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


def test_get_entity_nonexistent_returns_404(client: TestClient, db_dsn: str) -> None:
    """TC-003 — /entities/{nonexistent_uuid} → 404."""
    _set_master_flag(db_dsn, value=True)
    try:
        key_id, secret = _seed_api_key(db_dsn)
        resp = client.get(
            f"/v1/research/entities/{uuid4()}",
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp.status_code == 404
        detail = resp.json().get("detail", resp.json())
        assert detail.get("code") == "BITEMPORAL_PRE_BITEMPORAL_REGION"
    finally:
        _set_master_flag(db_dsn, value=False)


def test_get_disclosure_nonexistent_returns_404(client: TestClient, db_dsn: str) -> None:
    """TC-003 — /disclosures/{nonexistent_id} → 404."""
    _set_master_flag(db_dsn, value=True)
    try:
        key_id, secret = _seed_api_key(db_dsn)
        resp = client.get(
            f"/v1/research/disclosures/{uuid4()}",
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp.status_code == 404
        detail = resp.json().get("detail", resp.json())
        assert detail.get("code") == "BITEMPORAL_PRE_BITEMPORAL_REGION"
    finally:
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# 5. PII redaction on /events (TC-058 / TC-061, D30)
#
# A full HTTP-roundtrip seed of agg.filing_event would require seeding
# src.source, src.ingestion_run, doc.filing, and ref.entity rows with
# all their FK chains intact. The redaction logic itself lives in the
# pure helper ``_redact_event_payload``; per the existing test pattern
# in test_endpoints.py we exercise that contract directly. This is the
# same approach that test_endpoints.py uses for ``_parse_as_of``.
# ---------------------------------------------------------------------


def test_redact_event_payload_redacts_counterparty_for_default_principal() -> None:
    """TC-058 — payload counterparty_name redacted for principal w/o pii_unredacted.

    Per SCOPE.md D30: ``counterparty_name`` is replaced with the token
    ``<REDACTED:counterparty>`` unless the principal carries
    ``pii_unredacted=true``.
    """
    from aslan_core.api.research_auth import ApiKeyPrincipal
    from aslan_core.api.routes.research import _redact_event_payload

    principal = ApiKeyPrincipal(
        key_id=uuid4(),
        rate_tier="partner",
        pii_unredacted=False,
        scopes=(),
    )
    payload = {
        "counterparty_name": "Şişe Cam Topluluğu A.Ş.",
        "amount_try": 1_000_000,
    }
    result = _redact_event_payload(payload, principal)
    assert isinstance(result, dict)
    assert result["counterparty_name"] == "<REDACTED:counterparty>"
    # Non-PII fields untouched.
    assert result["amount_try"] == 1_000_000
    # Original literal must NOT leak.
    assert "Şişe Cam Topluluğu A.Ş." not in str(result)


def test_redact_event_payload_unredacted_for_pii_unredacted_principal() -> None:
    """TC-058 / TC-061 — pii_unredacted=true returns original counterparty."""
    from aslan_core.api.research_auth import ApiKeyPrincipal
    from aslan_core.api.routes.research import _redact_event_payload

    principal = ApiKeyPrincipal(
        key_id=uuid4(),
        rate_tier="partner",
        pii_unredacted=True,
        scopes=(),
    )
    payload = {
        "counterparty_name": "Şişe Cam Topluluğu A.Ş.",
        "amount_try": 1_000_000,
    }
    result = _redact_event_payload(payload, principal)
    assert isinstance(result, dict)
    assert result["counterparty_name"] == "Şişe Cam Topluluğu A.Ş."


# ---------------------------------------------------------------------
# 6. Pre-bitemporal provenance on /disclosures rows (TC-008 — revised)
# ---------------------------------------------------------------------


def test_disclosures_rows_carry_as_of_provenance(client: TestClient, db_dsn: str) -> None:
    """TC-008 (round-6 revision) — after migration 0051's SCD-4
    upgrade, ``/v1/research/disclosures`` no longer attaches a
    ``PRE_BITEMPORAL_TABLE`` envelope warning (the table IS bitemporal).
    Per-row provenance is surfaced via the ``as_of_provenance`` enum
    on each disclosure record; rows backfilled without a real
    ``as_of`` are tagged ``pre_bitemporal_unknown`` and additionally
    carry ``pre_bitemporal=true``.

    The ``PRE_BITEMPORAL_TABLE`` warning still surfaces on
    ``/v1/research/filings`` (no ``doc.filing_at`` PIT function in
    v1) — covered separately in
    ``test_filings_envelope_carries_pre_bitemporal_warning``.

    This test asserts the response shape, not specific values; an
    empty result set is acceptable.
    """
    _set_master_flag(db_dsn, value=True)
    try:
        key_id, secret = _seed_api_key(db_dsn)
        resp = client.get(
            "/v1/research/disclosures",
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp.status_code == 200
        body = resp.json()

        # /disclosures must NOT carry the PRE_BITEMPORAL_TABLE warning
        # any more — that warning is reserved for /filings (Class F).
        warnings = body.get("metadata", {}).get("warnings", [])
        codes = {w.get("code") for w in warnings}
        assert "PRE_BITEMPORAL_TABLE" not in codes, (
            "post-0051 /disclosures is bitemporal; warning belongs to /filings only"
        )

        # If any rows are present, every row must carry as_of_provenance.
        for row in body.get("data", []) or []:
            assert "as_of_provenance" in row
            assert row["as_of_provenance"] in {
                "live",
                "index_fetched_at",
                "body_fetched_at",
                "published_at",
                "pre_bitemporal_unknown",
            }
            if row["as_of_provenance"] == "pre_bitemporal_unknown":
                assert row.get("pre_bitemporal") is True
    finally:
        _set_master_flag(db_dsn, value=False)


def test_filings_envelope_carries_pre_bitemporal_warning(client: TestClient, db_dsn: str) -> None:
    """TC-008b — the PRE_BITEMPORAL_TABLE warning moved from
    /disclosures (now bitemporal via 0051) to /filings (Class F;
    no ``doc.filing_at`` PIT function in v1; v1.0.0-beta follow-up).
    """
    _set_master_flag(db_dsn, value=True)
    try:
        key_id, secret = _seed_api_key(db_dsn)
        resp = client.get(
            "/v1/research/filings",
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        warnings = body.get("metadata", {}).get("warnings", [])
        codes = {w.get("code") for w in warnings}
        assert "PRE_BITEMPORAL_TABLE" in codes
    finally:
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# 7. Quality-scores happy path envelope (TC-001 / TC-073)
#
# We do NOT seed an entity_quality_score row (would require seeding
# ref.entity + ref.currency + src.ingestion_run). With master flag on
# and a valid key, the endpoint must return a well-formed envelope
# with an empty data list and a populated ``metadata.as_of_resolved``.
# That is the contract guarantee from build_envelope.
# ---------------------------------------------------------------------


def test_quality_scores_envelope_shape_empty_result(client: TestClient, db_dsn: str) -> None:
    """TC-001 / TC-073 — /quality-scores envelope has ``data`` list +
    ``metadata.as_of_resolved`` even when the result set is empty.
    """
    _set_master_flag(db_dsn, value=True)
    try:
        key_id, secret = _seed_api_key(db_dsn)
        resp = client.get(
            f"/v1/research/quality-scores?entity_id={uuid4()}",
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert isinstance(body["data"], list)
        meta = body.get("metadata", {})
        assert "as_of_resolved" in meta
        assert meta["as_of_resolved"] is not None
    finally:
        _set_master_flag(db_dsn, value=False)
