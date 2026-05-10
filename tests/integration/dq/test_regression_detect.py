"""Tests for dq.regression_detect — v1 thresholds + v2 KAP correlation.

v1 path: seed an entity with two quarterly canonical_financial rows
where the QoQ shift exceeds the metric threshold; assert one
``audit.regression_flag`` row lands with status='open'.

v2 path: with the same v1-flag setup, additionally seed a recent
``kap.disclosures`` row of category 'material_event' for the same
entity within ``[detected_at - 7d, +1d]``. Run the combined
``detect_and_correlate``; assert the flag is auto-dismissed and the
``regression_auto_dismissed`` event lands.

The regression_detect module skips gracefully when the source tables
aren't present, so tests stand up real ts.canonical_financial /
ref.entity / ref.identifier rows and a synthetic kap.disclosures
table for the v2 path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.dq import regression_detect

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def _v1_seeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[UUID]:
    """Seed AKBNK with two quarterly is.revenue rows where the QoQ
    shift far exceeds the 25% threshold (100 → 200, +100%)."""
    async with session_factory() as s:
        await s.execute(text("DELETE FROM audit.regression_flag"))
        # Seed src.source / src.ingestion_run for FKs
        await s.execute(
            text(
                "INSERT INTO src.source(source_id, name, kind, license_status) "
                "VALUES ('bist', 'BIST', 'exchange', 'public') "
                "ON CONFLICT DO NOTHING"
            )
        )
        run_row = (
            await s.execute(
                text(
                    "INSERT INTO src.ingestion_run("
                    "  source_id, job_name, status, started_at"
                    ") VALUES ('bist', 'test-regression-detect', 'succeeded', now()) "
                    "RETURNING ingestion_run_id"
                )
            )
        ).one()
        run_id = run_row.ingestion_run_id

        eid = uuid4()
        await s.execute(
            text(
                "INSERT INTO ref.entity(entity_id, entity_type, status, "
                "  legal_name, country_code, source_id, ingestion_run_id) "
                "VALUES (:eid, 'company', 'active', 'AkbankRegTest', 'TR', "
                "        'bist', :rid)"
            ),
            {"eid": eid, "rid": run_id},
        )
        await s.execute(
            text("DELETE FROM ref.identifier WHERE namespace = 'bist_ticker' AND value = 'AKBNKRT'")
        )
        await s.execute(
            text(
                "INSERT INTO ref.identifier"
                "(entity_id, namespace, value, valid_from, valid_to, "
                " is_primary, source_id, ingestion_run_id) "
                "VALUES (:eid, 'bist_ticker', 'AKBNKRT', '2020-01-01', "
                "        '9999-12-31', true, 'bist', :rid)"
            ),
            {"eid": eid, "rid": run_id},
        )
        await s.execute(
            text(
                "INSERT INTO ref.currency(currency_code, name) "
                "VALUES ('TRY', 'Turkish Lira') ON CONFLICT DO NOTHING"
            )
        )
        for period_end, value in (
            (date(2026, 3, 31), Decimal("200000000")),  # current
            (date(2025, 12, 31), Decimal("100000000")),  # prior
        ):
            await s.execute(
                text(
                    "INSERT INTO ts.canonical_financial("
                    "  entity_id, canonical_code, period_end, period_type, "
                    "  consolidation, restatement_basis, currency_code, "
                    "  accounting_standard, value, payload_hash, "
                    "  source_contributions, mapping_version, manifest_hash, "
                    "  as_of, ingestion_run_id"
                    ") VALUES ("
                    "  :eid, 'is.revenue', :pe, 'q', 'consolidated', 'as_reported', "
                    "  'TRY', 'IFRS', :v, "
                    "  repeat('a', 64), '{}'::jsonb, 1, "
                    "  repeat('b', 64), now(), :rid"
                    ") ON CONFLICT DO NOTHING"
                ),
                {"eid": eid, "pe": period_end, "v": value, "rid": run_id},
            )
        await s.commit()
    yield eid
    async with session_factory() as s:
        await s.execute(
            text("DELETE FROM ts.canonical_financial WHERE entity_id = :eid"),
            {"eid": eid},
        )
        await s.execute(
            text(
                "DELETE FROM ref.identifier WHERE namespace = 'bist_ticker'   AND value = 'AKBNKRT'"
            )
        )
        await s.execute(text("DELETE FROM ref.entity WHERE entity_id = :eid"), {"eid": eid})
        await s.execute(text("DELETE FROM audit.regression_flag"))
        await s.commit()


async def test_v1_inserts_flag_for_threshold_breach(
    _v1_seeded: UUID,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """+100% QoQ on revenue (threshold 25%) → exactly one open flag."""
    eid = _v1_seeded
    async with session_factory() as s:
        results = await regression_detect.detect_v1(session=s)
        await s.commit()
    matched = [r for r in results if r.entity_id == eid and r.metric == "revenue"]
    assert len(matched) == 1
    async with session_factory() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, metric, shift_pct FROM audit.regression_flag "
                    "WHERE flag_id = :id"
                ),
                {"id": matched[0].flag_id},
            )
        ).one()
        assert row.status == "open"
        assert row.metric == "revenue"
        assert Decimal(str(row.shift_pct)) == Decimal("100")


@pytest_asyncio.fixture(loop_scope="session")
async def _kap_disclosures_for_v2(
    _v1_seeded: UUID,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[UUID]:
    """Synthetic doc.filing with one material_event filing on
    the seeded entity within the v2 correlation window.

    The production query reads from `doc.filing` (the canonical
    cross-source mirror — see crawl commit 824ad5c) rather than
    `kap.disclosures` directly, since `doc.filing.kind` is the
    already-projected conceptual category.
    """
    eid = _v1_seeded
    fid = uuid4()
    pub = datetime.now(UTC) - timedelta(days=2)
    async with session_factory() as s:
        # src.source has FK from src.ingestion_run; seed it if not present
        await s.execute(
            text(
                "INSERT INTO src.source(source_id, name, kind, license_status) "
                "VALUES ('kap', 'KAP', 'public', 'public_data') ON CONFLICT DO NOTHING"
            )
        )
        # ingestion_run row required by doc.filing FK
        run_row = (
            await s.execute(
                text(
                    "INSERT INTO src.ingestion_run(job_name, source_id, started_at) "
                    "VALUES ('regression_v2_test_fixture', 'kap', now()) "
                    "RETURNING ingestion_run_id"
                )
            )
        ).one()
        run_id = run_row.ingestion_run_id
        await s.execute(
            text(
                "INSERT INTO doc.filing("
                "  filing_id, source_id, source_filing_ref, kind, title, language, "
                "  published_at, primary_object_key, primary_mime, primary_sha256, "
                "  primary_bytes, entity_id, ingestion_run_id, revision_no, "
                "  actor_id, actor_kind"
                ") VALUES ("
                "  :fid, 'kap', :ref, 'material_event', 'v2 test fixture', 'tr', "
                "  :pub, 'k', 'text/html', :sha, 1, :eid, :run, 1, "
                "  'system:test', 'system'"
                ")"
            ),
            {
                "fid": fid,
                "ref": f"V2-FIXTURE-{fid.hex[:8]}",
                "pub": pub,
                "sha": "f" * 64,
                "eid": eid,
                "run": run_id,
            },
        )
        await s.commit()
    yield eid
    async with session_factory() as s:
        await s.execute(text("DELETE FROM doc.filing WHERE filing_id = :fid"), {"fid": fid})
        await s.execute(
            text("DELETE FROM src.ingestion_run WHERE ingestion_run_id = :run"),
            {"run": run_id},
        )
        await s.commit()


async def test_v2_auto_dismisses_when_kap_filing_present(
    _kap_disclosures_for_v2: UUID,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """v1 produces a flag; v2 finds the justifying KAP filing and
    auto-dismisses; ``regression_auto_dismissed`` event lands."""
    async with session_factory() as s:
        summary = await regression_detect.detect_and_correlate(session=s)
        await s.commit()
    assert summary.v1_flagged >= 1
    assert summary.v2_dismissed >= 1
    async with session_factory() as s:
        # Open flags should now be 0 (every v1 flag was dismissed)
        n_open = (
            await s.execute(
                text("SELECT count(*)::int AS n FROM audit.regression_flag WHERE status = 'open'")
            )
        ).scalar_one()
        assert int(n_open) == 0
        # And at least one auto-dismissal event landed
        n_events = (
            await s.execute(
                text(
                    "SELECT count(*)::int AS n FROM audit.event "
                    "WHERE event_type = 'regression_auto_dismissed'"
                )
            )
        ).scalar_one()
        assert int(n_events) >= 1


async def test_v2_keeps_flag_open_when_no_kap_filing(
    _v1_seeded: UUID,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without a recent justifying filing on doc.filing for the entity,
    v2 leaves the flag open."""
    async with session_factory() as s:
        # Purge any test-fixture filings and any existing flags so the
        # detector starts clean. doc.filing is shared with other tests,
        # so only delete v2-fixture-tagged rows.
        await s.execute(text("DELETE FROM doc.filing WHERE source_filing_ref LIKE 'V2-FIXTURE-%'"))
        await s.execute(text("DELETE FROM audit.regression_flag"))
        summary = await regression_detect.detect_and_correlate(session=s)
        await s.commit()
    assert summary.v1_flagged >= 1
    assert summary.v2_dismissed == 0
    assert summary.v2_kept_open == summary.v1_flagged


async def test_correlate_v2_window_boundary(
    _v1_seeded: UUID,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A doc.filing 8 days before detected_at falls outside the [-7d,+1d]
    window and does NOT auto-dismiss the flag."""
    eid = _v1_seeded
    fid = uuid4()
    async with session_factory() as s:
        # Purge any prior fixture rows so this test is independent.
        await s.execute(text("DELETE FROM doc.filing WHERE source_filing_ref LIKE 'V2-FIXTURE-%'"))
        await s.execute(
            text(
                "INSERT INTO src.source(source_id, name, kind, license_status) "
                "VALUES ('kap', 'KAP', 'public', 'public_data') ON CONFLICT DO NOTHING"
            )
        )
        run_row = (
            await s.execute(
                text(
                    "INSERT INTO src.ingestion_run(job_name, source_id, started_at) "
                    "VALUES ('regression_v2_window_test', 'kap', now()) "
                    "RETURNING ingestion_run_id"
                )
            )
        ).one()
        run_id = run_row.ingestion_run_id
        # Filing 8 days back → outside the [-7d, +1d] correlation window
        await s.execute(
            text(
                "INSERT INTO doc.filing("
                "  filing_id, source_id, source_filing_ref, kind, title, language, "
                "  published_at, primary_object_key, primary_mime, primary_sha256, "
                "  primary_bytes, entity_id, ingestion_run_id, revision_no, "
                "  actor_id, actor_kind"
                ") VALUES ("
                "  :fid, 'kap', :ref, 'material_event', 'window-edge test', 'tr', "
                "  now() - INTERVAL '8 days', 'k', 'text/html', :sha, 1, "
                "  :eid, :run, 1, 'system:test', 'system'"
                ")"
            ),
            {
                "fid": fid,
                "ref": f"V2-FIXTURE-WINDOW-{fid.hex[:8]}",
                "sha": "f" * 64,
                "eid": eid,
                "run": run_id,
            },
        )
        await s.execute(text("DELETE FROM audit.regression_flag"))
        await s.commit()
    try:
        async with session_factory() as s:
            now = datetime.now(UTC)
            v1 = await regression_detect.detect_v1(session=s, now=now)
            assert len(v1) >= 1
            v2 = await regression_detect.correlate_v2(session=s, flags=v1)
            await s.commit()
        assert all(not r.dismissed for r in v2)
    finally:
        async with session_factory() as s:
            await s.execute(text("DELETE FROM doc.filing WHERE filing_id = :fid"), {"fid": fid})
            await s.execute(
                text("DELETE FROM src.ingestion_run WHERE ingestion_run_id = :run"),
                {"run": run_id},
            )
            await s.commit()


async def test_metrics_constant_includes_six_entries() -> None:
    """The curated metric list matches spec §7.4 — 6 metrics."""
    assert len(regression_detect.METRICS) == 6
    metric_names = {m.metric for m in regression_detect.METRICS}
    assert metric_names == {
        "revenue",
        "net_income",
        "total_assets",
        "debt_to_equity",
        "pe",
        "roe",
    }


async def test_v1_window_constant() -> None:
    """v2 correlation window is 7d back / 1d forward."""
    back = regression_detect._V2_WINDOW_BACK
    forward = regression_detect._V2_WINDOW_FORWARD
    assert back == timedelta(days=7)
    assert forward == timedelta(days=1)
