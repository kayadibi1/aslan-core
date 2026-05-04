"""Integration tests for ``aslan_core.query.entity``.

Covers list_entities (shape, search, pagination, has_financials) and
get_entity_quality (public check output, ordering, limit bounds).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.query.entity import get_entity_quality, list_entities
from aslan_core.query.schemas import (
    AI_PRINCIPAL,
    EntitySummary,
    QualityScorePublic,
    RestatementBasis,
)

pytestmark = pytest.mark.integration

# Module-unique source_id avoids FK collisions with other test modules
# (e.g. document-store tests that seed doc.filing with source_id='kap').
_SRC = "test_qe_0099"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wipe(session: AsyncSession) -> None:
    """Delete rows seeded by ``_seed`` in FK-safe order.

    Scoped to ``source_id = _SRC`` so rows from other test modules
    are left untouched. A bare ``DELETE FROM src.ingestion_run`` fails
    with a FK violation when other modules' ``doc.filing`` rows still
    reference the same ingestion run.
    """
    # Leaf tables first (FK children), then parents.
    await session.execute(
        text(
            "DELETE FROM ts.entity_quality_score WHERE entity_id IN "
            "(SELECT entity_id FROM ref.entity WHERE source_id = :sid)"
        ),
        {"sid": _SRC},
    )
    await session.execute(
        text(
            "DELETE FROM ts.canonical_financial WHERE entity_id IN "
            "(SELECT entity_id FROM ref.entity WHERE source_id = :sid)"
        ),
        {"sid": _SRC},
    )
    await session.execute(
        text(
            "DELETE FROM ref.identifier WHERE entity_id IN "
            "(SELECT entity_id FROM ref.entity WHERE source_id = :sid)"
        ),
        {"sid": _SRC},
    )
    await session.execute(
        text("DELETE FROM ref.entity WHERE source_id = :sid"),
        {"sid": _SRC},
    )
    await session.execute(
        text("DELETE FROM src.ingestion_run WHERE source_id = :sid"),
        {"sid": _SRC},
    )
    await session.execute(
        text("DELETE FROM src.source WHERE source_id = :sid"),
        {"sid": _SRC},
    )
    await session.commit()


async def _seed(session: AsyncSession) -> tuple[UUID, UUID, int]:
    """Seed two entities with canonical financials and quality scores.

    Returns ``(entity_id_1, entity_id_2, ingestion_run_id)``.
    """
    await session.execute(
        text(
            "INSERT INTO src.source (source_id, name, kind, license_status) "
            "VALUES (:sid, 'TestQE', 'scraper', 'open') ON CONFLICT DO NOTHING"
        ),
        {"sid": _SRC},
    )
    run_id: int = (
        await session.execute(
            text(
                "INSERT INTO src.ingestion_run (source_id, job_name, status) "
                "VALUES (:sid, 'seed', 'succeeded') RETURNING ingestion_run_id"
            ),
            {"sid": _SRC},
        )
    ).scalar_one()

    # Currency required by canonical_financial FK
    await session.execute(
        text(
            "INSERT INTO ref.currency (currency_code, name) "
            "VALUES ('TRY', 'Turkish Lira') ON CONFLICT DO NOTHING"
        )
    )

    # Entity 1: Aselsan — has financials
    eid1: UUID = (
        await session.execute(
            text(
                "INSERT INTO ref.entity "
                "  (entity_type, legal_name, short_name, status, source_id, ingestion_run_id) "
                "VALUES ('company', 'Aselsan A.Ş.', 'Aselsan', 'active', :sid, :run) "
                "RETURNING entity_id"
            ),
            {"sid": _SRC, "run": run_id},
        )
    ).scalar_one()
    await session.execute(
        text(
            "INSERT INTO ref.identifier "
            "  (entity_id, namespace, value, is_primary, source_id, ingestion_run_id) "
            "VALUES (:eid, 'bist_ticker', 'ASELS', true, :sid, :run)"
        ),
        {"eid": eid1, "sid": _SRC, "run": run_id},
    )

    # Entity 2: Turkcell — has financials
    eid2: UUID = (
        await session.execute(
            text(
                "INSERT INTO ref.entity "
                "  (entity_type, legal_name, short_name, status, source_id, ingestion_run_id) "
                "VALUES ('company', 'Turkcell İletişim Hiz. A.Ş.', 'Turkcell', 'active', "
                "        :sid, :run) "
                "RETURNING entity_id"
            ),
            {"sid": _SRC, "run": run_id},
        )
    ).scalar_one()
    await session.execute(
        text(
            "INSERT INTO ref.identifier "
            "  (entity_id, namespace, value, is_primary, source_id, ingestion_run_id) "
            "VALUES (:eid, 'bist_ticker', 'TCELL', true, :sid, :run)"
        ),
        {"eid": eid2, "sid": _SRC, "run": run_id},
    )

    # Canonical financials for entity 1
    now = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO ts.canonical_financial "
            "  (entity_id, canonical_code, period_end, period_type, consolidation, "
            "   restatement_basis, currency_code, accounting_standard, value, "
            "   payload_hash, source_contributions, mapping_version, manifest_hash, "
            "   as_of, ingestion_run_id) "
            "VALUES "
            "  (:eid, 'revenue', '2024-03-31', 'q', 'consolidated', "
            "   'as_reported', 'TRY', 'TFRS', 100000, :hash, '{}', 1, :hash, "
            "   :now, :run), "
            "  (:eid, 'net_income', '2024-06-30', 'q', 'consolidated', "
            "   'as_reported', 'TRY', 'TFRS', 50000, :hash, '{}', 1, :hash, "
            "   :now, :run)"
        ),
        {
            "eid": eid1,
            "hash": "a" * 64,
            "now": now,
            "run": run_id,
        },
    )

    # Canonical financials for entity 2
    await session.execute(
        text(
            "INSERT INTO ts.canonical_financial "
            "  (entity_id, canonical_code, period_end, period_type, consolidation, "
            "   restatement_basis, currency_code, accounting_standard, value, "
            "   payload_hash, source_contributions, mapping_version, manifest_hash, "
            "   as_of, ingestion_run_id) "
            "VALUES "
            "  (:eid, 'revenue', '2024-03-31', 'q', 'consolidated', "
            "   'as_reported', 'TRY', 'TFRS', 200000, :hash, '{}', 1, :hash, "
            "   :now, :run)"
        ),
        {
            "eid": eid2,
            "hash": "b" * 64,
            "now": now,
            "run": run_id,
        },
    )

    # Quality scores for entity 1
    checks_json = (
        '{"checks": {"balance_check": {"state": "pass", "delta_pct": 0.01, '
        '"coverage_pct": 0.95, "internal_detail": "do_not_expose"}}}'
    )
    await session.execute(
        text(
            "INSERT INTO ts.entity_quality_score "
            "  (entity_id, period_end, period_type, consolidation, currency_code, "
            "   accounting_standard, restatement_basis, score, insufficient_data, "
            "   checks, mapping_version, manifest_hash, as_of, ingestion_run_id) "
            "VALUES "
            "  (:eid, '2024-03-31', 'q', 'consolidated', 'TRY', 'TFRS', "
            "   'as_reported', 85, false, CAST(:checks AS jsonb), 1, :hash, :now, :run), "
            "  (:eid, '2024-06-30', 'q', 'consolidated', 'TRY', 'TFRS', "
            "   'as_reported', 90, false, CAST(:checks AS jsonb), 1, :hash, :now, :run)"
        ),
        {
            "eid": eid1,
            "checks": checks_json,
            "hash": "a" * 64,
            "now": now,
            "run": run_id,
        },
    )

    await session.commit()
    return eid1, eid2, run_id


# ---------------------------------------------------------------------------
# list_entities tests
# ---------------------------------------------------------------------------


async def test_list_entities_returns_correct_shape(session: AsyncSession) -> None:
    await _wipe(session)
    eid1, _eid2, _ = await _seed(session)

    rows, total = await list_entities(session, AI_PRINCIPAL)
    assert total >= 2
    assert len(rows) >= 2
    assert all(isinstance(r, EntitySummary) for r in rows)

    # Check fields on the Aselsan row
    aselsan = next(r for r in rows if r.entity_id == eid1)
    assert aselsan.legal_name == "Aselsan A.Ş."
    assert aselsan.ticker == "ASELS"
    assert aselsan.latest_period == date(2024, 6, 30)
    assert aselsan.canonical_line_count == 2
    assert aselsan.avg_quality_score is not None


async def test_list_entities_search_filters_by_name(session: AsyncSession) -> None:
    await _wipe(session)
    await _seed(session)

    rows, total = await list_entities(session, AI_PRINCIPAL, search="Aselsan")
    assert total == 1
    assert len(rows) == 1
    assert rows[0].legal_name == "Aselsan A.Ş."


async def test_list_entities_search_case_insensitive(session: AsyncSession) -> None:
    await _wipe(session)
    await _seed(session)

    rows, _ = await list_entities(session, AI_PRINCIPAL, search="aselsan")
    assert len(rows) == 1
    assert rows[0].legal_name == "Aselsan A.Ş."


async def test_list_entities_pagination(session: AsyncSession) -> None:
    await _wipe(session)
    await _seed(session)

    page1, total = await list_entities(session, AI_PRINCIPAL, limit=1, offset=0)
    assert len(page1) == 1
    assert total >= 2

    page2, _ = await list_entities(session, AI_PRINCIPAL, limit=1, offset=1)
    assert len(page2) == 1
    # Pages must not overlap
    assert page1[0].entity_id != page2[0].entity_id


async def test_list_entities_has_financials_false(session: AsyncSession) -> None:
    """When has_financials=False, entities without canonical financials appear."""
    await _wipe(session)
    _eid1, _eid2, run_id = await _seed(session)

    # Add a third entity with no financials
    await session.execute(
        text(
            "INSERT INTO ref.entity "
            "  (entity_type, legal_name, status, source_id, ingestion_run_id) "
            "VALUES ('company', 'No Financials Co', 'active', :sid, :run)"
        ),
        {"sid": _SRC, "run": run_id},
    )
    await session.commit()

    _rows_with, total_with = await list_entities(session, AI_PRINCIPAL, has_financials=True)
    _rows_without, total_without = await list_entities(session, AI_PRINCIPAL, has_financials=False)
    assert total_without > total_with


async def test_list_entities_limit_bounds_enforced(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="limit must be 1-100"):
        await list_entities(session, AI_PRINCIPAL, limit=0)
    with pytest.raises(ValueError, match="limit must be 1-100"):
        await list_entities(session, AI_PRINCIPAL, limit=101)


async def test_list_entities_offset_bounds_enforced(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="offset must be 0-10000"):
        await list_entities(session, AI_PRINCIPAL, offset=-1)
    with pytest.raises(ValueError, match="offset must be 0-10000"):
        await list_entities(session, AI_PRINCIPAL, offset=10_001)


async def test_list_entities_search_length_enforced(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="search string must be at most 100"):
        await list_entities(session, AI_PRINCIPAL, search="x" * 101)


# ---------------------------------------------------------------------------
# get_entity_quality tests
# ---------------------------------------------------------------------------


async def test_get_entity_quality_returns_public_check_output(
    session: AsyncSession,
) -> None:
    await _wipe(session)
    eid1, _, _ = await _seed(session)

    results = await get_entity_quality(session, AI_PRINCIPAL, eid1)
    assert len(results) == 2
    assert all(isinstance(r, QualityScorePublic) for r in results)

    # Most recent first
    assert results[0].period_end >= results[1].period_end

    # Check allowlisted fields present, internal fields stripped
    check = results[0].checks["balance_check"]
    assert check.state == "pass"
    assert check.delta_pct == pytest.approx(0.01)
    assert check.coverage_pct == pytest.approx(0.95)
    # The QualityCheckPublic model only has state/delta_pct/coverage_pct —
    # internal_detail must not appear
    assert not hasattr(check, "internal_detail")


async def test_get_entity_quality_respects_restatement_basis(
    session: AsyncSession,
) -> None:
    await _wipe(session)
    eid1, _, _ = await _seed(session)

    # Only as_reported scores were seeded — cpi_normalized should be empty
    results = await get_entity_quality(
        session, AI_PRINCIPAL, eid1, restatement_basis=RestatementBasis.CPI_NORMALIZED
    )
    assert results == []


async def test_get_entity_quality_limit_bounds_enforced(
    session: AsyncSession,
) -> None:
    from uuid import uuid4

    with pytest.raises(ValueError, match="limit must be 1-100"):
        await get_entity_quality(session, AI_PRINCIPAL, uuid4(), limit=0)
    with pytest.raises(ValueError, match="limit must be 1-100"):
        await get_entity_quality(session, AI_PRINCIPAL, uuid4(), limit=101)
