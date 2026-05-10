"""Unit tests for the five per-source recency probes.

Each probe is exercised with a fake AsyncSession that records the
SQL queries it receives and returns canned rows. No DB.

Real testcontainer roundtrip lives in
`tests/integration/dq/test_recency_sweep_cli.py` — these unit tests
focus on the probe-level branching logic (table-present vs absent,
placeholder timestamp shape, probe_detail keys).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from aslan_core.dq.probes import (
    SOURCES,
    BistProbe,
    EvdsProbe,
    KapProbe,
    MkkProbe,
    Probe,
    TefasProbe,
    get_probe,
)


def _fake_session_with_results(results: list[Any]) -> AsyncMock:
    """Build an AsyncSession-shaped mock that pops one canned result
    per `execute(...)` call.

    `results` is a list of pre-built `result.one()`-shaped objects (a
    MagicMock with the attributes the probe expects on the row, e.g.
    `.present`, `.db_latest_at`, `.expected_at`).
    """
    session = AsyncMock()
    canned = list(results)

    async def _execute(*args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        result = MagicMock()
        result.one = MagicMock(return_value=canned.pop(0))
        return result

    session.execute = _execute
    return session


def _row(**fields: Any) -> MagicMock:
    """Build a row mock with attribute access — matches what
    `(await session.execute(...)).one()` returns under SQLAlchemy."""
    row = MagicMock(spec=list(fields.keys()))
    for key, value in fields.items():
        setattr(row, key, value)
    return row


# ── Protocol conformance ───────────────────────────────────────────


@pytest.mark.parametrize(
    "probe_cls",
    [KapProbe, EvdsProbe, BistProbe, TefasProbe, MkkProbe],
)
def test_each_probe_is_protocol_compliant(probe_cls: type) -> None:
    instance = probe_cls()
    assert isinstance(instance, Probe)
    assert hasattr(instance, "source")
    assert isinstance(instance.source, str)


def test_get_probe_returns_correct_instance() -> None:
    assert isinstance(get_probe("kap"), KapProbe)
    assert isinstance(get_probe("evds"), EvdsProbe)
    assert isinstance(get_probe("bist"), BistProbe)
    assert isinstance(get_probe("tefas"), TefasProbe)
    assert isinstance(get_probe("mkk"), MkkProbe)


def test_get_probe_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="unknown source"):
        get_probe("not-a-source")


def test_sources_tuple_is_complete() -> None:
    assert set(SOURCES) == {"kap", "evds", "bist", "tefas", "mkk"}


# ── KAP probe ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kap_db_latest_present() -> None:
    db_ts = datetime(2026, 5, 9, 10, 0, tzinfo=UTC)
    session = _fake_session_with_results([_row(present=True), _row(db_latest_at=db_ts)])
    probe = KapProbe()
    ts, detail = await probe.db_latest(session, "publish_to_db")
    assert ts == db_ts
    assert detail["table_present"] is True
    assert detail["table"] == "kap.disclosures"


@pytest.mark.asyncio
async def test_kap_db_latest_absent() -> None:
    session = _fake_session_with_results([_row(present=False)])
    probe = KapProbe()
    ts, detail = await probe.db_latest(session, "publish_to_db")
    assert ts is None
    assert detail["table_present"] is False
    assert detail["schema"] == "kap"
    assert detail["table"] == "disclosures"


@pytest.mark.asyncio
async def test_kap_upstream_db_only_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default config (DQ_KAP_LISTING_URL unset) -> DB-only mode."""
    monkeypatch.delenv("DQ_KAP_LISTING_URL", raising=False)
    db_ts = datetime(2026, 5, 9, 10, 0, tzinfo=UTC)
    session = _fake_session_with_results([_row(present=True), _row(db_latest_at=db_ts)])
    probe = KapProbe()
    ts, detail = await probe.upstream_latest(session, "publish_to_db")
    assert ts == db_ts
    assert detail["probe"] == "db_only"
    assert detail["mode"] == "db_only_default"
    assert detail["table_present"] is True


@pytest.mark.asyncio
async def test_kap_upstream_db_only_when_table_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB-only mode + missing table -> (None, table_present=False)."""
    monkeypatch.delenv("DQ_KAP_LISTING_URL", raising=False)
    session = _fake_session_with_results([_row(present=False)])
    probe = KapProbe()
    ts, detail = await probe.upstream_latest(session, "publish_to_db")
    assert ts is None
    assert detail["table_present"] is False


def test_kap_parse_listing_top_level_list() -> None:
    """Top-level list shape returns max publishDate."""
    from aslan_core.dq.probes.kap import _parse_kap_listing

    payload = [
        {"publishDate": "2026-05-09 10:00:00", "title": "x"},
        {"publishDate": "2026-05-09 11:30:00", "title": "y"},
        {"publishDate": "2026-05-09 09:15:00", "title": "z"},
    ]
    assert _parse_kap_listing(payload) == datetime(2026, 5, 9, 11, 30, tzinfo=UTC)


def test_kap_parse_listing_envelope_data() -> None:
    """Envelope shape ``{"data": [...]}`` also works."""
    from aslan_core.dq.probes.kap import _parse_kap_listing

    payload = {"data": [{"publishDate": "2026-05-09T08:00:00Z"}]}
    assert _parse_kap_listing(payload) == datetime(2026, 5, 9, 8, 0, tzinfo=UTC)


def test_kap_parse_listing_unknown_shape_returns_none() -> None:
    """Unrecognised shape -> None so caller can fall back to DB-only."""
    from aslan_core.dq.probes.kap import _parse_kap_listing

    assert _parse_kap_listing({"unexpected": "shape"}) is None
    assert _parse_kap_listing("string-payload") is None
    assert _parse_kap_listing([{"no": "publishDate"}]) is None


# ── EVDS probe ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evds_db_latest_present() -> None:
    db_ts = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    session = _fake_session_with_results([_row(present=True), _row(db_latest_at=db_ts)])
    probe = EvdsProbe()
    ts, detail = await probe.db_latest(session, "release_window")
    assert ts == db_ts
    assert detail["table_present"] is True


@pytest.mark.asyncio
async def test_evds_upstream_reads_calendar() -> None:
    expected_ts = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)
    session = _fake_session_with_results([_row(expected_at=expected_ts)])
    probe = EvdsProbe()
    ts, detail = await probe.upstream_latest(session, "release_window")
    assert ts == expected_ts
    assert detail["calendar_table"] == "audit.evds_release_calendar"


@pytest.mark.asyncio
async def test_evds_upstream_handles_empty_calendar() -> None:
    session = _fake_session_with_results([_row(expected_at=None)])
    probe = EvdsProbe()
    ts, _detail = await probe.upstream_latest(session, "release_window")
    assert ts is None


# ── BIST probe ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bist_db_latest_present() -> None:
    db_ts = datetime(2026, 5, 8, 0, 0, tzinfo=UTC)
    session = _fake_session_with_results([_row(present=True), _row(db_latest_at=db_ts)])
    probe = BistProbe()
    ts, _ = await probe.db_latest(session, "trade_close_to_ohlcv")
    assert ts == db_ts


@pytest.mark.asyncio
async def test_bist_db_latest_absent_returns_none() -> None:
    session = _fake_session_with_results([_row(present=False)])
    probe = BistProbe()
    ts, detail = await probe.db_latest(session, "trade_close_to_ohlcv")
    assert ts is None
    assert detail["table_present"] is False


@pytest.mark.asyncio
async def test_bist_upstream_returns_recent_weekday_close() -> None:
    session = _fake_session_with_results([])
    probe = BistProbe()
    ts, detail = await probe.upstream_latest(session, "trade_close_to_ohlcv")
    assert ts is not None
    assert ts.weekday() < 5
    # 15:00 UTC close
    assert ts.hour == 15
    assert ts.minute == 0
    # Within last week
    assert (datetime.now(UTC) - ts) <= timedelta(days=7)
    assert detail["probe"] == "placeholder"


# ── TEFAS probe ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tefas_db_latest_present() -> None:
    db_ts = datetime(2026, 5, 7, 0, 0, tzinfo=UTC)
    session = _fake_session_with_results([_row(present=True), _row(db_latest_at=db_ts)])
    probe = TefasProbe()
    ts, _ = await probe.db_latest(session, "per_fund_cadence")
    assert ts == db_ts


@pytest.mark.asyncio
async def test_tefas_upstream_two_days_back() -> None:
    session = _fake_session_with_results([])
    probe = TefasProbe()
    ts, _ = await probe.upstream_latest(session, "per_fund_cadence")
    assert ts is not None
    delta = datetime.now(UTC) - ts
    assert abs(delta.total_seconds() - timedelta(days=2).total_seconds()) < 5


# ── MKK probe ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mkk_db_latest_present() -> None:
    db_ts = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)
    session = _fake_session_with_results([_row(present=True), _row(db_latest_at=db_ts)])
    probe = MkkProbe()
    ts, _ = await probe.db_latest(session, "event_at_to_db")
    assert ts == db_ts


@pytest.mark.asyncio
async def test_mkk_upstream_one_day_back() -> None:
    session = _fake_session_with_results([])
    probe = MkkProbe()
    ts, _ = await probe.upstream_latest(session, "event_at_to_db")
    assert ts is not None
    delta = datetime.now(UTC) - ts
    assert abs(delta.total_seconds() - timedelta(days=1).total_seconds()) < 5
