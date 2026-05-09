"""Phase 4 — append-only trigger H1 tests.

Per ``docs/specs/bitemporal-research-api/SCOPE.md`` D29 (Trigger tests).

For every Class A bitemporal table registered in
``aslan_core.bitemporal_table_registry`` with ``exposed_in_api=true``:
seed a row, attempt UPDATE, assert PostgreSQL raises
``feature_not_supported`` (SQLSTATE ``0A000``). Then attempt to insert
a duplicate ``(entity, as_of)`` and assert the unique constraint fires.

Cases (per docs/specs/bitemporal-research-api/TESTPLAN.md):

- TC-022 — UPDATE on ts.canonical_financial raises feature_not_supported.
- TC-023 / TC-026 — duplicate (entity, as_of) on Class A table fails.
- TC-027 (data shape) — observation: UPDATE rejected at trigger level.
- TC-028 (data shape) — financial_line_item: UPDATE rejected.

The H1 trigger contract: ``aslan_core.reject_bitemporal_update_generic``
applied as ``<table>_no_update`` BEFORE UPDATE on every Class A table
(migration 0044).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(loop_scope="session")
async def _trigger_world(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES ('trig_test', 'trig_test', 'scraper', 'open') "
            "ON CONFLICT DO NOTHING"
        )
    )
    await session.execute(
        text(
            "INSERT INTO src.ingestion_run "
            "(ingestion_run_id, source_id, job_name, status) "
            "VALUES (777002, 'trig_test', 'trig_seed', 'succeeded') "
            "ON CONFLICT DO NOTHING"
        )
    )
    await session.execute(
        text(
            "INSERT INTO ref.currency (currency_code, name) "
            "VALUES ('TRY', 'Turkish Lira') ON CONFLICT DO NOTHING"
        )
    )
    await session.commit()


def _is_feature_not_supported(exc: BaseException) -> bool:
    """Return True if the exception chain reports SQLSTATE 0A000."""
    cur: BaseException | None = exc
    while cur is not None:
        sqlstate = getattr(cur, "sqlstate", None) or getattr(
            cur, "pgcode", None
        )
        if sqlstate == "0A000":
            return True
        cur = cur.__cause__ or cur.__context__
    return False


# ---------------------------------------------------------------------
# TC-022..028 — UPDATE rejected on every Class A table
# ---------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_update_on_ts_observation_rejected(
    session: AsyncSession, _trigger_world: None
) -> None:
    """TC-026/TC-027 — UPDATE on ts.observation raises feature_not_supported."""
    await session.execute(
        text(
            "INSERT INTO ts.series_catalog "
            "(series_code, source_id, metric, frequency, unit) "
            "VALUES ('trig_obs', 'trig_test', 'm', '1d', 'TRY') "
            "ON CONFLICT DO NOTHING"
        )
    )
    sid = await session.scalar(
        text(
            "SELECT series_id FROM ts.series_catalog WHERE series_code='trig_obs'"
        )
    )
    await session.execute(
        text(
            "INSERT INTO ts.observation "
            "(series_id, ts, as_of, value, ingestion_run_id, payload_hash) "
            "VALUES (:sid, '2024-06-15T13:00:00Z', '2024-06-15T13:30:00Z', "
            "        42.10, 777002, repeat('a', 64))"
        ),
        {"sid": sid},
    )
    await session.commit()

    with pytest.raises(DBAPIError) as ei:
        await session.execute(
            text("UPDATE ts.observation SET value = 99.0 WHERE series_id = :sid"),
            {"sid": sid},
        )
        await session.commit()
    await session.rollback()
    assert _is_feature_not_supported(ei.value), (
        f"expected SQLSTATE 0A000 (feature_not_supported), got {ei.value!r}"
    )

    # Cleanup
    await session.execute(
        text("DELETE FROM ts.observation WHERE series_id = :sid"),
        {"sid": sid},
    )
    await session.execute(
        text("DELETE FROM ts.series_catalog WHERE series_code='trig_obs'")
    )
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_update_on_ts_canonical_financial_rejected(
    session: AsyncSession, _trigger_world: None
) -> None:
    """TC-022 — UPDATE on ts.canonical_financial raises feature_not_supported."""
    eid = uuid4()
    await session.execute(
        text(
            "INSERT INTO ref.entity "
            "(entity_id, entity_type, legal_name, status, "
            " source_id, ingestion_run_id) "
            "VALUES (:eid, 'company', 'Trig Canon Corp', 'active', "
            "        'trig_test', 777002)"
        ),
        {"eid": eid},
    )
    await session.execute(
        text(
            "INSERT INTO ts.canonical_financial "
            "(entity_id, canonical_code, period_end, period_type, "
            " consolidation, restatement_basis, currency_code, "
            " accounting_standard, value, payload_hash, source_contributions, "
            " mapping_version, manifest_hash, as_of, "
            " ingestion_run_id, cpi_base_date) "
            "VALUES (:eid, 'revenue', '2024-12-31', 'y', 'consolidated', "
            "        'as_reported', 'TRY', 'ifrs', 100.00, repeat('a', 64), "
            "        '{}', 1, repeat('b', 64), '2025-01-15T08:00:00Z', "
            "        777002, '9999-01-01')"
        ),
        {"eid": eid},
    )
    await session.commit()

    with pytest.raises(DBAPIError) as ei:
        await session.execute(
            text(
                "UPDATE ts.canonical_financial SET value = 99.0 "
                "WHERE entity_id = :eid"
            ),
            {"eid": eid},
        )
        await session.commit()
    await session.rollback()
    assert _is_feature_not_supported(ei.value), (
        f"expected SQLSTATE 0A000, got {ei.value!r}"
    )

    # Cleanup
    await session.execute(
        text("DELETE FROM ts.canonical_financial WHERE entity_id = :eid"),
        {"eid": eid},
    )
    await session.execute(
        text("DELETE FROM ref.entity WHERE entity_id = :eid"),
        {"eid": eid},
    )
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_update_on_ref_identifier_rejected(
    session: AsyncSession, _trigger_world: None
) -> None:
    """TC-022 (per-Class-A) — UPDATE on ref.identifier raises.

    ref.identifier is daterange-bitemporal but the trigger applies just
    the same — version advancement is via new-row insert, never UPDATE.
    """
    eid = uuid4()
    await session.execute(
        text(
            "INSERT INTO ref.entity "
            "(entity_id, entity_type, legal_name, status, "
            " source_id, ingestion_run_id) "
            "VALUES (:eid, 'company', 'Trig Ident Corp', 'active', "
            "        'trig_test', 777002)"
        ),
        {"eid": eid},
    )
    await session.execute(
        text(
            "INSERT INTO ref.identifier "
            "(entity_id, namespace, value, valid_from, valid_to, "
            " is_primary, source_id, ingestion_run_id) "
            "VALUES (:eid, 'BIST', 'TRIGTST', '2018-01-01', '2099-01-01', "
            "        true, 'trig_test', 777002)"
        ),
        {"eid": eid},
    )
    await session.commit()

    with pytest.raises(DBAPIError) as ei:
        await session.execute(
            text(
                "UPDATE ref.identifier SET is_primary = false "
                "WHERE entity_id = :eid AND namespace = 'BIST'"
            ),
            {"eid": eid},
        )
        await session.commit()
    await session.rollback()
    assert _is_feature_not_supported(ei.value), (
        f"expected SQLSTATE 0A000, got {ei.value!r}"
    )

    await session.execute(
        text("DELETE FROM ref.identifier WHERE entity_id = :eid"),
        {"eid": eid},
    )
    await session.execute(
        text("DELETE FROM ref.entity WHERE entity_id = :eid"),
        {"eid": eid},
    )
    await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_every_class_a_table_has_no_update_trigger(
    session: AsyncSession,
) -> None:
    """TC-061 / TC-066 — registry-driven sweep: every Class A row has its
    BEFORE UPDATE trigger named ``<table>_no_update`` per the D28 contract.

    Asserts the registry is non-empty and each row's trigger is wired.
    """
    rows = (
        await session.execute(
            text(
                "SELECT schema_name, table_name "
                "FROM aslan_core.bitemporal_table_registry "
                "ORDER BY schema_name, table_name"
            )
        )
    ).all()
    assert len(rows) >= 5, f"registry should have ≥5 rows, got {len(rows)}"

    for r in rows:
        present = await session.scalar(
            text(
                "SELECT count(*) FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :sn AND c.relname = :tn "
                "  AND t.tgname = :trg AND NOT t.tgisinternal"
            ),
            {
                "sn": r.schema_name,
                "tn": r.table_name,
                "trg": f"{r.table_name}_no_update",
            },
        )
        assert int(present or 0) >= 1, (
            f"missing trigger {r.table_name}_no_update on "
            f"{r.schema_name}.{r.table_name}"
        )


# ---------------------------------------------------------------------
# TC-026 / TC-038 — duplicate (entity, as_of) blocked
# ---------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_duplicate_observation_pk_blocked(
    session: AsyncSession, _trigger_world: None
) -> None:
    """TC-026 — duplicate (series_id, ts, as_of) on ts.observation fails.

    Inserting the exact same triple with a different value must raise an
    IntegrityError; the only way to record a revision is a new ``as_of``.
    """
    await session.execute(
        text(
            "INSERT INTO ts.series_catalog "
            "(series_code, source_id, metric, frequency, unit) "
            "VALUES ('trig_dup', 'trig_test', 'm', '1d', 'TRY') "
            "ON CONFLICT DO NOTHING"
        )
    )
    sid = await session.scalar(
        text(
            "SELECT series_id FROM ts.series_catalog WHERE series_code='trig_dup'"
        )
    )
    await session.execute(
        text(
            "INSERT INTO ts.observation "
            "(series_id, ts, as_of, value, ingestion_run_id, payload_hash) "
            "VALUES (:sid, '2024-06-15T13:00:00Z', '2024-06-15T13:30:00Z', "
            "        42.10, 777002, repeat('a', 64))"
        ),
        {"sid": sid},
    )
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO ts.observation "
                "(series_id, ts, as_of, value, ingestion_run_id, payload_hash) "
                "VALUES (:sid, '2024-06-15T13:00:00Z', '2024-06-15T13:30:00Z', "
                "        99.0, 777002, repeat('a', 64))"
            ),
            {"sid": sid},
        )
        await session.commit()
    await session.rollback()

    # Cleanup
    await session.execute(
        text("DELETE FROM ts.observation WHERE series_id = :sid"),
        {"sid": sid},
    )
    await session.execute(
        text("DELETE FROM ts.series_catalog WHERE series_code='trig_dup'")
    )
    await session.commit()
