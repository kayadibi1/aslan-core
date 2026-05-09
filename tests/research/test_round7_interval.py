"""Round-7 — interval-mode (``as_of_range``) integration tests.

Round 6 added the interval-mode parser, route gating, and the
``_PIT_TO_PHYSICAL_TABLE`` rewrite that lets list endpoints scan SCD-4
history tables directly. Round 7 fixed several contract issues:

- ``BITEMPORAL_API_INTERVAL_QUERIES`` feature-flag gating;
- ORDER BY <natural-key>, as_of ASC for version-chain rows;
- half-open ``[T1,T2)`` semantics in the WHERE predicate
  (``as_of >= :as_of_lower AND as_of < :as_of_upper``);
- cache-control basis = ``interval[1]`` (not ``now()``) so historical
  intervals get the immutable cache-control header;
- per-row temporal correctness for ``/entities`` lineage joins.

Round 6 closed the parser-level test suite (see ``test_round6.py``);
this module closes the *SQL-path* test suite by seeding multi-version
data and asserting the actual ``data`` array, ordering, cardinality,
and cache headers returned by the route.

Test cases per ``docs/specs/bitemporal-research-api/TESTPLAN.md``:

- TC-014 — interval mode returns the full version chain in ascending
  ``as_of`` order.
- TC-015 — half-open semantics: T1 inclusive, T2 exclusive.
- TC-016 — empty interval returns 200 with empty ``data``.
- TC-017 — ``ref.entity_version`` interval mode returns the version
  chain with per-row lineage.
- TC-018 — ``kap.disclosures_version`` interval mode returns the
  version chain with ``as_of_provenance`` populated.
- TC-019 — interval cost limit triggers ``413 QUERY_TOO_LARGE``
  through the HTTP route (round-6 covered the parser-level test).
- TC-074 — ``BITEMPORAL_API_INTERVAL_QUERIES=false`` returns
  ``503 FEATURE_DISABLED`` on interval queries while PIT mode keeps
  serving 200.
- TC-D17 — interval cache basis: historical interval gets
  ``Cache-Control: public, ..., immutable``; interval extending into
  the present-day window gets a ``no-cache``-shaped header.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from aslan_core.api.routes.research import router as research_router
from aslan_core.db.engine import create_engine as _create_engine

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------
# Fixtures (mirrored from tests/research/test_round6.py).
# ---------------------------------------------------------------------


def _libpq_dsn(asyncpg_dsn: str) -> str:
    """Convert SQLAlchemy +asyncpg DSN to a psycopg-compatible libpq URL."""
    if asyncpg_dsn.startswith("postgresql+asyncpg://"):
        return "postgresql://" + asyncpg_dsn[len("postgresql+asyncpg://") :]
    return asyncpg_dsn


@pytest.fixture()
def app(pg_dsn: str) -> Iterator[FastAPI]:
    """FastAPI app with only the research router mounted (round-7 tests)."""

    @asynccontextmanager
    async def _lifespan(a: FastAPI) -> AsyncIterator[None]:
        a.state.engine = _create_engine(pg_dsn)
        try:
            yield
        finally:
            await a.state.engine.dispose()

    a = FastAPI(
        title="Aslan Research API (test-round7)", version="test", lifespan=_lifespan
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


def _set_interval_flag(dsn: str, *, value: bool) -> None:
    """Toggle BITEMPORAL_API_INTERVAL_QUERIES in aslan_core.feature_flags."""
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE aslan_core.feature_flags "
            "SET value_bool = %s, updated_at = now(), updated_by = 'tests' "
            "WHERE flag_name = 'BITEMPORAL_API_INTERVAL_QUERIES'",
            (value,),
        )
        conn.commit()


def _seed_api_key(dsn: str, *, pii_unredacted: bool = False) -> tuple[str, str]:
    """Seed an aslan_core.api_key row and return (key_id, secret).

    Mirrors :func:`tests.research.test_round6._seed_api_key` so this
    file stays self-contained per the round-6/7 fixture-discipline rule
    ("don't add new pytest fixtures").
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
# Seed helpers
# ---------------------------------------------------------------------

# Fictitious test scaffolding so tear-down is unambiguous (per the
# round-7 brief: "pick fictitious entity_ids / canonical_codes ...
# so tear-down is unambiguous"). We pin a deterministic UUID for the
# entity so successive seed calls in the same test address the same
# bitemporal key.
_TEST_SOURCE_ID = "tc014-source"
_TEST_CANONICAL_CODE = "TEST-CC-001"


def _seed_canonical_financial_versions(
    dsn: str,
    *,
    entity_id: str,
    canonical_code: str,
    versions: list[tuple[str, float]],
) -> int:
    """Seed N rows for the same bitemporal key at distinct ``as_of``.

    ``versions`` is a list of ``(as_of_iso, value)`` tuples. All rows
    share the natural key (entity_id, canonical_code, period_end='2024-12-31',
    period_type='y', consolidation='consolidated', currency_code='TRY',
    accounting_standard='ifrs', restatement_basis='as_reported',
    cpi_base_date='9999-01-01', mapping_version=1) so they form a single
    version chain in ``ts.canonical_financial``.

    Returns the ingestion_run_id used (so tear-down can DELETE by it).
    """
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (%s, 'TC-014 Test Source', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING",
            (_TEST_SOURCE_ID,),
        )
        # ingestion_run_id is BIGSERIAL — let DB assign and capture it.
        cur.execute(
            "INSERT INTO src.ingestion_run (source_id, job_name, status) "
            "VALUES (%s, 'tc014-seed', 'succeeded') "
            "RETURNING ingestion_run_id",
            (_TEST_SOURCE_ID,),
        )
        run_row = cur.fetchone()
        assert run_row is not None
        run_id = int(run_row[0])
        cur.execute(
            "INSERT INTO ref.currency (currency_code, name) "
            "VALUES ('TRY', 'Turkish Lira') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO ref.entity "
            "(entity_id, entity_type, legal_name, status, source_id, ingestion_run_id) "
            "VALUES (%s, 'company', 'TC-014 Test Co.', 'active', %s, %s) "
            "ON CONFLICT DO NOTHING",
            (entity_id, _TEST_SOURCE_ID, run_id),
        )
        for as_of_iso, value in versions:
            cur.execute(
                "INSERT INTO ts.canonical_financial "
                "(entity_id, canonical_code, period_end, period_type, "
                " consolidation, restatement_basis, currency_code, "
                " accounting_standard, value, payload_hash, "
                " source_contributions, mapping_version, manifest_hash, "
                " as_of, ingestion_run_id) "
                "VALUES (%s, %s, '2024-12-31', 'y', 'consolidated', "
                "        'as_reported', 'TRY', 'ifrs', %s, %s, '{}', 1, "
                "        %s, %s, %s)",
                (
                    entity_id,
                    canonical_code,
                    value,
                    "a" * 64,
                    "b" * 64,
                    as_of_iso,
                    run_id,
                ),
            )
        conn.commit()
    return run_id


def _wipe_canonical_financial_seed(
    dsn: str, *, entity_id: str, canonical_code: str, run_id: int
) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM ts.canonical_financial "
            "WHERE entity_id = %s AND canonical_code = %s",
            (entity_id, canonical_code),
        )
        # Migration 0046 installs an AFTER DELETE trigger on ref.entity
        # that inserts a 'deleted' version row into ref.entity_version.
        # Delete the parent first so the trigger emits its row, THEN
        # wipe ref.entity_version (catches the trigger-emitted row plus
        # any others). Pass-3 finding 6.
        cur.execute(
            "DELETE FROM ref.entity WHERE entity_id = %s",
            (entity_id,),
        )
        cur.execute(
            "DELETE FROM ref.entity_version WHERE entity_id = %s",
            (entity_id,),
        )
        cur.execute(
            "DELETE FROM src.ingestion_run WHERE ingestion_run_id = %s",
            (run_id,),
        )
        cur.execute(
            "DELETE FROM src.source WHERE source_id = %s",
            (_TEST_SOURCE_ID,),
        )
        conn.commit()


def _seed_entity_versions(
    dsn: str, *, entity_id: str, versions: list[tuple[str, str]]
) -> int:
    """Seed rows directly in ``ref.entity_version``.

    Per the round-7 brief, we INSERT directly into ``ref.entity_version``
    rather than going through the AFTER trigger on ``ref.entity`` so the
    test ``as_of`` values are deterministic (the trigger uses ``now()``).

    ``versions`` is a list of ``(as_of_iso, legal_name)`` tuples.

    Returns the ingestion_run_id used so tear-down can DELETE by it.
    """
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (%s, 'TC-017 Test Source', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING",
            (_TEST_SOURCE_ID,),
        )
        cur.execute(
            "INSERT INTO src.ingestion_run (source_id, job_name, status) "
            "VALUES (%s, 'tc017-seed', 'succeeded') "
            "RETURNING ingestion_run_id",
            (_TEST_SOURCE_ID,),
        )
        run_row = cur.fetchone()
        assert run_row is not None
        run_id = int(run_row[0])
        # ref.entity row so the entity_id is referentially valid for any
        # downstream join. Without this the version rows themselves are
        # still valid (no FK from entity_version to entity), but the
        # /entities route's lineage subquery walks ref.entity_lineage_at
        # which is keyed off entity_id — keeping the parent in place is
        # the safer shape.
        cur.execute(
            "INSERT INTO ref.entity "
            "(entity_id, entity_type, legal_name, status, source_id, ingestion_run_id) "
            "VALUES (%s, 'company', %s, 'active', %s, %s) "
            "ON CONFLICT DO NOTHING",
            (entity_id, versions[0][1], _TEST_SOURCE_ID, run_id),
        )
        # Drop the rows the AFTER trigger on ref.entity just emitted so
        # only our deterministic versions remain.
        cur.execute(
            "DELETE FROM ref.entity_version WHERE entity_id = %s",
            (entity_id,),
        )
        for as_of_iso, legal_name in versions:
            cur.execute(
                "INSERT INTO ref.entity_version "
                "(entity_id, as_of, event_kind, entity_type, legal_name, "
                " country_code, status, metadata, source_id, ingestion_run_id) "
                "VALUES (%s, %s, 'updated', 'company', %s, 'TR', 'active', "
                "        '{}'::jsonb, %s, %s)",
                (entity_id, as_of_iso, legal_name, _TEST_SOURCE_ID, run_id),
            )
        conn.commit()
    return run_id


def _wipe_entity_seed(dsn: str, *, entity_id: str, run_id: int) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        # Migration 0046 installs an AFTER DELETE trigger on ref.entity
        # that inserts a 'deleted' version row into ref.entity_version.
        # Delete the parent first so the trigger emits its row, THEN
        # wipe ref.entity_version (catches the trigger-emitted row plus
        # any others). Pass-3 finding 6.
        cur.execute(
            "DELETE FROM ref.entity WHERE entity_id = %s",
            (entity_id,),
        )
        cur.execute(
            "DELETE FROM ref.entity_version WHERE entity_id = %s",
            (entity_id,),
        )
        cur.execute(
            "DELETE FROM src.ingestion_run WHERE ingestion_run_id = %s",
            (run_id,),
        )
        cur.execute(
            "DELETE FROM src.source WHERE source_id = %s",
            (_TEST_SOURCE_ID,),
        )
        conn.commit()


def _seed_disclosure_versions(
    dsn: str,
    *,
    disclosure_id: str,
    entity_id: str,
    versions: list[tuple[str, str]],
) -> int:
    """Seed rows directly in ``kap.disclosures_version``.

    Per the round-7 brief: we INSERT directly into the SCD-4 history
    table; ``kap.disclosures_version`` accepts INSERT without going
    through ``kap.disclosures`` (the trigger fires on the parent, not
    the version table). The ref.entity must exist for the FK-shaped
    columns to be referentially harmless (no FK declared from
    disclosures_version to entity, but we keep the row for hygiene).

    ``versions`` is a list of ``(as_of_iso, title)`` tuples.

    Returns the ingestion_run_id used so tear-down can DELETE by it.
    """
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (%s, 'TC-018 Test Source', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING",
            (_TEST_SOURCE_ID,),
        )
        cur.execute(
            "INSERT INTO src.ingestion_run (source_id, job_name, status) "
            "VALUES (%s, 'tc018-seed', 'succeeded') "
            "RETURNING ingestion_run_id",
            (_TEST_SOURCE_ID,),
        )
        run_row = cur.fetchone()
        assert run_row is not None
        run_id = int(run_row[0])
        cur.execute(
            "INSERT INTO ref.entity "
            "(entity_id, entity_type, legal_name, status, source_id, ingestion_run_id) "
            "VALUES (%s, 'company', 'TC-018 Test Co.', 'active', %s, %s) "
            "ON CONFLICT DO NOTHING",
            (entity_id, _TEST_SOURCE_ID, run_id),
        )
        for as_of_iso, title in versions:
            cur.execute(
                "INSERT INTO kap.disclosures_version "
                "(disclosure_id, as_of, event_kind, as_of_provenance, "
                " entity_id, kap_id, published_at, category_code, "
                " title, language, kap_url, is_amendment, body_fetched, "
                " index_fetched_at, raw_index_storage_key) "
                "VALUES (%s, %s, 'indexed', 'live', %s, 'KAP-TC018-CO', "
                "        %s, 'ODA', %s, 'tr', "
                "        'https://example.invalid/tc018', false, false, "
                "        %s, 'kap/tc018/index')",
                (
                    disclosure_id,
                    as_of_iso,
                    entity_id,
                    as_of_iso,
                    title,
                    as_of_iso,
                ),
            )
        conn.commit()
    return run_id


def _wipe_disclosure_seed(
    dsn: str, *, disclosure_id: str, entity_id: str, run_id: int
) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM kap.disclosures_version WHERE disclosure_id = %s",
            (disclosure_id,),
        )
        # Migration 0046 installs an AFTER DELETE trigger on ref.entity
        # that inserts a 'deleted' version row into ref.entity_version.
        # Delete the parent first so the trigger emits its row, THEN
        # wipe ref.entity_version (catches the trigger-emitted row plus
        # any others). Pass-3 finding 6.
        cur.execute(
            "DELETE FROM ref.entity WHERE entity_id = %s",
            (entity_id,),
        )
        cur.execute(
            "DELETE FROM ref.entity_version WHERE entity_id = %s",
            (entity_id,),
        )
        cur.execute(
            "DELETE FROM src.ingestion_run WHERE ingestion_run_id = %s",
            (run_id,),
        )
        cur.execute(
            "DELETE FROM src.source WHERE source_id = %s",
            (_TEST_SOURCE_ID,),
        )
        conn.commit()


# ---------------------------------------------------------------------
# TC-014 — interval mode returns full version chain in ascending as_of
# ---------------------------------------------------------------------


def test_canonical_financial_interval_returns_version_chain_ascending(
    client: TestClient, db_dsn: str
) -> None:
    """TC-014 — ``/financials/canonical?as_of_range=...`` returns the
    full version chain ordered by ``as_of`` ASC.

    Three rows for the same bitemporal key at three distinct ``as_of``
    values must all surface, with ``data[0].as_of < data[1].as_of <
    data[2].as_of``. The metadata envelope echoes the requested
    ``as_of_range``. Closes BUG_REVIEW_FINDINGS_PASS2 round-7 finding 8
    on the canonical_financial endpoint.
    """
    _set_master_flag(db_dsn, value=True)
    entity_id = str(uuid4())
    versions = [
        ("2024-04-15T08:00:00+00:00", 1500.00),
        ("2024-05-10T11:00:00+00:00", 1487.50),
        ("2024-06-20T16:00:00+00:00", 1495.25),
    ]
    run_id: int | None = None
    try:
        run_id = _seed_canonical_financial_versions(
            db_dsn,
            entity_id=entity_id,
            canonical_code=_TEST_CANONICAL_CODE,
            versions=versions,
        )
        key_id, secret = _seed_api_key(db_dsn)
        resp = client.get(
            "/v1/research/financials/canonical",
            params={
                "entity_id": entity_id,
                "as_of_range": "[2024-04-01T00:00:00Z,2024-07-01T00:00:00Z)",
            },
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Filter to rows for our test canonical_code so we don't trip
        # on shared-DB pollution from other tests.
        rows = [
            r for r in body["data"] if r.get("canonical_code") == _TEST_CANONICAL_CODE
        ]
        assert len(rows) == 3, f"expected 3 versions, got {len(rows)}: {rows}"
        # Ascending as_of (TC-014 core invariant).
        assert rows[0]["as_of"] < rows[1]["as_of"] < rows[2]["as_of"]
        # Values were 1500.00, 1487.50, 1495.25 in chronological order.
        assert rows[0]["value"] == pytest.approx(1500.00)
        assert rows[1]["value"] == pytest.approx(1487.50)
        assert rows[2]["value"] == pytest.approx(1495.25)
        # Envelope echoes the interval.
        as_of_range = body.get("metadata", {}).get("as_of_range")
        assert as_of_range is not None
        assert isinstance(as_of_range, list) and len(as_of_range) == 2
        # ISO-form starts with the requested date; tolerant of trailing
        # offset shape ("Z" vs "+00:00") since the route normalizes.
        assert as_of_range[0].startswith("2024-04-01T00:00:00")
        assert as_of_range[1].startswith("2024-07-01T00:00:00")
    finally:
        if run_id is not None:
            _wipe_canonical_financial_seed(
                db_dsn,
                entity_id=entity_id,
                canonical_code=_TEST_CANONICAL_CODE,
                run_id=run_id,
            )
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# TC-015 — half-open semantics: T1 inclusive, T2 exclusive
# ---------------------------------------------------------------------


def test_canonical_financial_interval_half_open_semantics(
    client: TestClient, db_dsn: str
) -> None:
    """TC-015 — ``[T1,T2)`` includes T1 and excludes T2.

    Two rows seeded at exact endpoints (``T1 = 2024-05-01T00:00:00Z``
    and ``T2 = 2024-06-01T00:00:00Z``); querying
    ``as_of_range=[T1,T2)`` must return exactly the row at T1.
    """
    _set_master_flag(db_dsn, value=True)
    entity_id = str(uuid4())
    versions = [
        ("2024-05-01T00:00:00+00:00", 1000.00),  # T1 — included
        ("2024-06-01T00:00:00+00:00", 2000.00),  # T2 — excluded
    ]
    run_id: int | None = None
    try:
        run_id = _seed_canonical_financial_versions(
            db_dsn,
            entity_id=entity_id,
            canonical_code=_TEST_CANONICAL_CODE,
            versions=versions,
        )
        key_id, secret = _seed_api_key(db_dsn)
        resp = client.get(
            "/v1/research/financials/canonical",
            params={
                "entity_id": entity_id,
                "as_of_range": "[2024-05-01T00:00:00Z,2024-06-01T00:00:00Z)",
            },
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        rows = [
            r for r in body["data"] if r.get("canonical_code") == _TEST_CANONICAL_CODE
        ]
        assert len(rows) == 1, (
            f"expected exactly 1 row in [T1,T2) (T1 inclusive, T2 exclusive); "
            f"got {len(rows)}: {rows}"
        )
        assert rows[0]["value"] == pytest.approx(1000.00)
        # The included row's as_of is the T1 endpoint itself.
        assert rows[0]["as_of"].startswith("2024-05-01T00:00:00")
    finally:
        if run_id is not None:
            _wipe_canonical_financial_seed(
                db_dsn,
                entity_id=entity_id,
                canonical_code=_TEST_CANONICAL_CODE,
                run_id=run_id,
            )
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# TC-016 — empty range returns 200 with empty data
# ---------------------------------------------------------------------


def test_canonical_financial_interval_empty_range_returns_200(
    client: TestClient, db_dsn: str
) -> None:
    """TC-016 — interval entirely outside any seeded data returns 200,
    ``data == []``, populated ``metadata.as_of_range``, no error.

    Future endpoints (year 2099) avoid hitting the future-as_of guard
    because the guard targets PIT ``as_of`` only; interval-mode
    endpoints can range into the future.
    """
    _set_master_flag(db_dsn, value=True)
    entity_id = str(uuid4())
    try:
        key_id, secret = _seed_api_key(db_dsn)
        resp = client.get(
            "/v1/research/financials/canonical",
            params={
                "entity_id": entity_id,
                "as_of_range": "[2099-01-01T00:00:00Z,2099-02-01T00:00:00Z)",
            },
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data"] == []
        as_of_range = body.get("metadata", {}).get("as_of_range")
        assert as_of_range is not None
        assert as_of_range[0].startswith("2099-01-01T00:00:00")
        assert as_of_range[1].startswith("2099-02-01T00:00:00")
    finally:
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# TC-017 — ref.entity_version interval mode
# ---------------------------------------------------------------------


def test_entities_interval_returns_version_chain_with_lineage(
    client: TestClient, db_dsn: str
) -> None:
    """TC-017 — ``/entities?as_of_range=...`` returns the version chain
    from ``ref.entity_version`` in ascending ``as_of`` order, each row
    carrying a ``lineage_events`` field (possibly empty).
    """
    _set_master_flag(db_dsn, value=True)
    entity_id = str(uuid4())
    versions = [
        ("2024-03-01T09:00:00+00:00", "TC-017 Original Co."),
        ("2024-04-15T15:30:00+00:00", "TC-017 Renamed Co."),
    ]
    run_id: int | None = None
    try:
        run_id = _seed_entity_versions(
            db_dsn, entity_id=entity_id, versions=versions
        )
        key_id, secret = _seed_api_key(db_dsn)
        resp = client.get(
            "/v1/research/entities",
            params={
                "as_of_range": "[2024-02-01T00:00:00Z,2024-05-01T00:00:00Z)",
                "limit": 500,
            },
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        rows = [r for r in body["data"] if r.get("entity_id") == entity_id]
        assert len(rows) == 2, (
            f"expected 2 versions for our entity; got {len(rows)}: {rows}"
        )
        # Ascending as_of (TC-014/TC-017 core invariant).
        assert rows[0]["as_of"] < rows[1]["as_of"]
        # Each row carries lineage_events (may be empty).
        for r in rows:
            assert "lineage_events" in r
            assert isinstance(r["lineage_events"], list)
        # Newer version reflects the renamed legal_name.
        assert rows[0]["canonical_name"] == "TC-017 Original Co."
        assert rows[1]["canonical_name"] == "TC-017 Renamed Co."
    finally:
        if run_id is not None:
            _wipe_entity_seed(db_dsn, entity_id=entity_id, run_id=run_id)
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# TC-018 — kap.disclosures_version interval mode
# ---------------------------------------------------------------------


def test_disclosures_interval_returns_version_chain(
    client: TestClient, db_dsn: str
) -> None:
    """TC-018 — ``/disclosures?as_of_range=...`` returns the version
    chain from ``kap.disclosures_version`` ordered by ``as_of`` ASC,
    with ``as_of_provenance`` populated on every row.
    """
    _set_master_flag(db_dsn, value=True)
    entity_id = str(uuid4())
    disclosure_id = f"KAP-TC018-{uuid4().hex[:8]}"
    versions = [
        ("2024-03-15T10:00:00+00:00", "TC-018 — initial title"),
        ("2024-04-20T14:00:00+00:00", "TC-018 — amended title"),
    ]
    run_id: int | None = None
    try:
        run_id = _seed_disclosure_versions(
            db_dsn,
            disclosure_id=disclosure_id,
            entity_id=entity_id,
            versions=versions,
        )
        key_id, secret = _seed_api_key(db_dsn)
        resp = client.get(
            "/v1/research/disclosures",
            params={
                "as_of_range": "[2024-02-01T00:00:00Z,2024-06-01T00:00:00Z)",
                "entity_id": entity_id,
                "limit": 500,
            },
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        rows = [
            r for r in body["data"] if r.get("disclosure_id") == disclosure_id
        ]
        assert len(rows) == 2, (
            f"expected 2 versions for disclosure_id={disclosure_id}; "
            f"got {len(rows)}: {rows}"
        )
        # Ascending as_of.
        assert rows[0]["as_of"] < rows[1]["as_of"]
        # as_of_provenance populated on every row (round-7 contract).
        for r in rows:
            assert r.get("as_of_provenance") in {
                "live",
                "index_fetched_at",
                "body_fetched_at",
                "published_at",
                "pre_bitemporal_unknown",
            }
        assert rows[0]["title"] == "TC-018 — initial title"
        assert rows[1]["title"] == "TC-018 — amended title"
    finally:
        if run_id is not None:
            _wipe_disclosure_seed(
                db_dsn,
                disclosure_id=disclosure_id,
                entity_id=entity_id,
                run_id=run_id,
            )
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# TC-019 — interval cost limit triggers QUERY_TOO_LARGE through HTTP
# ---------------------------------------------------------------------


def test_observations_interval_16y_returns_query_too_large(
    client: TestClient, db_dsn: str
) -> None:
    """TC-019 — interval width > 5 years on ``/observations`` returns
    ``413 QUERY_TOO_LARGE`` through the HTTP route (round-6 covered the
    parser-level test; this is the HTTP-path complement).
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
        # RFC 7807 envelope per the research handler — code at top level.
        detail = body.get("detail", body)
        code = body.get("code") or (
            detail.get("code") if isinstance(detail, dict) else None
        )
        assert code == "QUERY_TOO_LARGE"
    finally:
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# TC-074 — BITEMPORAL_API_INTERVAL_QUERIES=false → 503 on interval,
# 200 on PIT
# ---------------------------------------------------------------------


def test_interval_flag_off_returns_503_on_interval_but_200_on_pit(
    client: TestClient, db_dsn: str
) -> None:
    """TC-074 — when ``BITEMPORAL_API_INTERVAL_QUERIES=false``, interval
    queries must return ``503 FEATURE_DISABLED`` while PIT queries keep
    serving 200. Restoring the flag in ``finally`` keeps shared-DB
    state hygienic.
    """
    _set_master_flag(db_dsn, value=True)
    _set_interval_flag(db_dsn, value=False)
    try:
        key_id, secret = _seed_api_key(db_dsn)
        # Interval mode → 503 FEATURE_DISABLED with the exact flag name.
        resp_interval = client.get(
            "/v1/research/observations",
            params={
                "as_of_range": "[2024-01-01T00:00:00Z,2024-02-01T00:00:00Z)",
            },
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp_interval.status_code == 503, resp_interval.text
        body = resp_interval.json()
        detail = body.get("detail", body)
        code = body.get("code") or (
            detail.get("code") if isinstance(detail, dict) else None
        )
        assert code == "FEATURE_DISABLED"
        # The exact flag name must surface in extensions per round-7
        # gating. The envelope shape varies — check both top-level and
        # detail-nested locations.
        ext = body.get("extensions") or (
            detail.get("extensions") if isinstance(detail, dict) else None
        )
        assert ext is not None, body
        assert ext.get("flag") == "BITEMPORAL_API_INTERVAL_QUERIES"

        # PIT mode → 200 (the master flag is still on; only the interval
        # sub-flag is off, so PIT reads keep working).
        resp_pit = client.get(
            "/v1/research/observations",
            params={"as_of": "2024-01-15T00:00:00Z"},
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp_pit.status_code == 200, resp_pit.text
    finally:
        # Restore the interval flag to its default (true) so the rest of
        # the suite isn't perturbed by leftover state.
        _set_interval_flag(db_dsn, value=True)
        _set_master_flag(db_dsn, value=False)


# ---------------------------------------------------------------------
# TC-D17 — interval cache basis (Cache-Control hinges on T2)
# ---------------------------------------------------------------------


def test_interval_cache_basis_uses_upper_bound_for_cache_control(
    client: TestClient, db_dsn: str
) -> None:
    """TC-D17 — interval cache freshness hinges on ``interval[1]``
    (the upper bound), not on ``now()``. A historical interval (T2 well
    in the past) gets the immutable cache-control header; an interval
    extending into the present-day window gets the no-cache header.
    Documents the round-7 cache-basis fix.
    """
    _set_master_flag(db_dsn, value=True)
    try:
        key_id, secret = _seed_api_key(db_dsn)
        # Past interval: T2 = 2010-02-01 is well past now()-5min, so the
        # cache-control header must be the "public, ..., immutable" form.
        resp_past = client.get(
            "/v1/research/observations",
            params={
                "as_of_range": "[2010-01-01T00:00:00Z,2010-02-01T00:00:00Z)",
            },
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp_past.status_code == 200, resp_past.text
        cc_past = resp_past.headers.get("Cache-Control", "")
        assert cc_past.startswith("public,"), (
            f"expected immutable cache-control for historical interval; got {cc_past!r}"
        )
        assert "immutable" in cc_past, (
            f"expected 'immutable' token in Cache-Control; got {cc_past!r}"
        )

        # Present-day interval: T2 = now()+1y, which is after the
        # 5-minute immutable threshold, so the cache header must be
        # the "no-cache, must-revalidate" shape.
        future_t2 = (datetime.now(tz=UTC) + timedelta(days=365)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        resp_now = client.get(
            "/v1/research/observations",
            params={
                "as_of_range": f"[2010-01-01T00:00:00Z,{future_t2})",
            },
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        # Spans 16+ years — guard the cost cap. Either the cost cap
        # fires (413) or the cache header is present-day. The round-7
        # cache-basis fix lives at ``cache_as_of = interval[1] if
        # interval is not None else resolved`` — and that line runs
        # only if cost-enforcement passes. Use a narrower interval to
        # keep this test focused on cache, not cost.
        assert resp_now.status_code in (200, 413), resp_now.text

        # Re-issue with a width <= 5y so cost gate doesn't fire.
        # Pick T1 = (now - 1 day), T2 = (now + 30 days). Width ~= 31d.
        present_t1 = (datetime.now(tz=UTC) - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        present_t2 = (datetime.now(tz=UTC) + timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        resp_present = client.get(
            "/v1/research/observations",
            params={
                "as_of_range": f"[{present_t1},{present_t2})",
            },
            headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
        )
        assert resp_present.status_code == 200, resp_present.text
        cc_present = resp_present.headers.get("Cache-Control", "")
        # Round-7 cache basis: T2 in the future (>>5min from now)
        # means the upper bound's "age" is negative, so set_cache_headers
        # falls into the no-cache branch.
        assert "no-cache" in cc_present, (
            f"expected 'no-cache' for present-day interval; got {cc_present!r}"
        )
    finally:
        _set_master_flag(db_dsn, value=False)
