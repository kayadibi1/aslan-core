"""Unit tests for per-table validation rules.

Pure-Python: rules return RuleResult lists for given record dicts;
the surrounding `validation.check()` (DB-backed) is exercised in
`tests/integration/dq/test_validation_roundtrip.py`.

Coverage discipline: per spec Appendix B, every rule fires at least
once on a fail-path record and stays silent on a happy-path record.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import ModuleType
from typing import Any

from aslan_core.dq.rules import (
    bist_daily_ohlcv,
    evds_observation,
    kap_disclosures,
    mkk_capital_action,
    tefas_fund_holding,
    ts_canonical_financial,
    ts_financial_line_item,
)
from aslan_core.dq.rules._types import RuleResult
from aslan_core.dq.types import Severity


# Helper: run all rules in a module against a record.
def _run_all(rules_module: ModuleType, record: dict[str, Any]) -> list[RuleResult]:
    out: list[RuleResult] = []
    for rule in rules_module.RULES:
        out.extend(rule.run(record))
    return out


# ---------------------------------------------------------------------------
# ts.canonical_financial
# ---------------------------------------------------------------------------


def test_canon_fin_happy_path_silent() -> None:
    record = {
        "entity_id": 1,
        "period_end": "2025-12-31T00:00:00Z",
        "as_of": "2026-01-15T00:00:00Z",
        "revenue": 1000.0,
        "net_income": 100.0,
    }
    assert _run_all(ts_canonical_financial, record) == []


def test_canon_fin_required_fields_fire() -> None:
    record: dict[str, Any] = {}
    results = ts_canonical_financial._required_fields(record)
    names = {r.rule_name for r in results}
    assert names == {"required_entity_id", "required_period_end", "required_as_of"}
    assert all(r.severity is Severity.ERROR for r in results)


def test_canon_fin_required_silent_when_present() -> None:
    record = {
        "entity_id": 1,
        "period_end": "2025-12-31T00:00:00Z",
        "as_of": "2026-01-15T00:00:00Z",
    }
    assert ts_canonical_financial._required_fields(record) == []


def test_canon_fin_revenue_negative_errors() -> None:
    results = ts_canonical_financial._revenue_range({"revenue": -1.0})
    assert len(results) == 1
    assert results[0].rule_name == "revenue_non_negative"
    assert results[0].severity is Severity.ERROR


def test_canon_fin_revenue_non_negative_silent() -> None:
    assert ts_canonical_financial._revenue_range({"revenue": 0.0}) == []
    assert ts_canonical_financial._revenue_range({"revenue": 12345.6}) == []
    assert ts_canonical_financial._revenue_range({}) == []


def test_canon_fin_net_income_range_extreme_errors() -> None:
    assert (
        ts_canonical_financial._net_income_range({"net_income": 1e16})[0].rule_name
        == "net_income_range"
    )
    assert (
        ts_canonical_financial._net_income_range({"net_income": -1e16})[0].rule_name
        == "net_income_range"
    )


def test_canon_fin_net_income_range_silent_in_band() -> None:
    assert ts_canonical_financial._net_income_range({"net_income": 0.0}) == []
    assert ts_canonical_financial._net_income_range({"net_income": 1e10}) == []
    assert ts_canonical_financial._net_income_range({}) == []


def test_canon_fin_net_income_exceeds_revenue_warn() -> None:
    results = ts_canonical_financial._net_income_le_revenue({"revenue": 100, "net_income": 200})
    assert len(results) == 1
    assert results[0].rule_name == "net_income_exceeds_revenue"
    assert results[0].severity is Severity.WARN


def test_canon_fin_net_income_le_revenue_silent_when_le_or_missing() -> None:
    assert ts_canonical_financial._net_income_le_revenue({"revenue": 100, "net_income": 100}) == []
    assert ts_canonical_financial._net_income_le_revenue({"revenue": 100, "net_income": 50}) == []
    # Missing one side: silent (rule is cross-field; need both).
    assert ts_canonical_financial._net_income_le_revenue({"revenue": 100}) == []
    assert ts_canonical_financial._net_income_le_revenue({"net_income": 100}) == []


def test_canon_fin_period_end_in_future_errors() -> None:
    future = (datetime.now(UTC) + timedelta(days=400)).isoformat()
    results = ts_canonical_financial._period_end_temporal({"period_end": future})
    assert len(results) == 1
    assert results[0].rule_name == "period_end_in_future"
    assert results[0].severity is Severity.ERROR


def test_canon_fin_period_end_past_silent() -> None:
    past = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    assert ts_canonical_financial._period_end_temporal({"period_end": past}) == []
    assert ts_canonical_financial._period_end_temporal({}) == []


def test_canon_fin_period_end_naive_datetime_treated_as_utc() -> None:
    naive_future = datetime.now() + timedelta(days=400)  # noqa: DTZ005
    results = ts_canonical_financial._period_end_temporal({"period_end": naive_future})
    assert len(results) == 1
    assert results[0].rule_name == "period_end_in_future"


# ---------------------------------------------------------------------------
# ts.financial_line_item
# ---------------------------------------------------------------------------


def test_line_item_happy_path_silent() -> None:
    record = {
        "canonical_financial_id": 1,
        "line_item": "revenue",
        "value": 1000.0,
    }
    assert _run_all(ts_financial_line_item, record) == []


def test_line_item_required_fields_fire() -> None:
    results = ts_financial_line_item._required_fields({})
    names = {r.rule_name for r in results}
    assert names == {
        "required_canonical_financial_id",
        "required_line_item",
        "required_value",
    }


def test_line_item_value_not_numeric_errors() -> None:
    results = ts_financial_line_item._value_numeric({"value": "abc"})
    assert len(results) == 1
    assert results[0].rule_name == "value_not_numeric"
    assert results[0].severity is Severity.ERROR


def test_line_item_value_bool_rejected() -> None:
    """bool is a subclass of int; rule excludes it explicitly."""
    results = ts_financial_line_item._value_numeric({"value": True})
    assert len(results) == 1
    assert results[0].rule_name == "value_not_numeric"


def test_line_item_value_numeric_silent() -> None:
    assert ts_financial_line_item._value_numeric({"value": 1}) == []
    assert ts_financial_line_item._value_numeric({"value": 1.5}) == []
    assert ts_financial_line_item._value_numeric({"value": -3.2}) == []
    # Missing value: required_fields owns that signal; _value_numeric stays silent.
    assert ts_financial_line_item._value_numeric({}) == []


# ---------------------------------------------------------------------------
# kap.disclosures
# ---------------------------------------------------------------------------


def test_kap_happy_path_silent() -> None:
    record = {
        "disclosure_id": "abc-123",
        "entity_id": 1,
        "published_at": "2026-01-01T00:00:00Z",
    }
    assert _run_all(kap_disclosures, record) == []


def test_kap_required_fields_fire() -> None:
    results = kap_disclosures._required_fields({})
    names = {r.rule_name for r in results}
    assert names == {
        "required_disclosure_id",
        "required_entity_id",
        "required_published_at",
    }


def test_kap_published_at_future_errors() -> None:
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    results = kap_disclosures._published_at_temporal({"published_at": future})
    assert len(results) == 1
    assert results[0].rule_name == "published_at_in_future"
    assert results[0].severity is Severity.ERROR


def test_kap_published_at_past_silent() -> None:
    past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    assert kap_disclosures._published_at_temporal({"published_at": past}) == []
    assert kap_disclosures._published_at_temporal({}) == []


def test_kap_published_at_accepts_datetime_object() -> None:
    past = datetime.now(UTC) - timedelta(days=2)
    assert kap_disclosures._published_at_temporal({"published_at": past}) == []


# ---------------------------------------------------------------------------
# evds.observation
# ---------------------------------------------------------------------------


def test_evds_happy_path_silent() -> None:
    record = {
        "series_code": "TP.DK.USD.A",
        "observation_date": "2026-01-01",
        "value": 32.5,
    }
    assert _run_all(evds_observation, record) == []


def test_evds_required_fields_fire() -> None:
    results = evds_observation._required_fields({})
    names = {r.rule_name for r in results}
    assert names == {
        "required_series_code",
        "required_observation_date",
        "required_value",
    }


def test_evds_observation_date_future_errors() -> None:
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    results = evds_observation._observation_date_temporal({"observation_date": future})
    assert len(results) == 1
    assert results[0].rule_name == "observation_date_in_future"


def test_evds_observation_date_past_silent() -> None:
    past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    assert evds_observation._observation_date_temporal({"observation_date": past}) == []
    assert evds_observation._observation_date_temporal({}) == []


def test_evds_observation_date_accepts_date_object() -> None:
    past = (datetime.now(UTC) - timedelta(days=10)).date()
    assert evds_observation._observation_date_temporal({"observation_date": past}) == []
    future_date: date = (datetime.now(UTC) + timedelta(days=10)).date()
    results = evds_observation._observation_date_temporal({"observation_date": future_date})
    assert len(results) == 1


# ---------------------------------------------------------------------------
# bist.daily_ohlcv
# ---------------------------------------------------------------------------


def test_bist_happy_path_silent() -> None:
    record = {
        "security_id": 1,
        "trade_date": "2026-01-01",
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": 10.5,
        "volume": 1000,
    }
    assert _run_all(bist_daily_ohlcv, record) == []


def test_bist_required_fields_fire() -> None:
    results = bist_daily_ohlcv._required_fields({})
    names = {r.rule_name for r in results}
    assert names == {
        "required_security_id",
        "required_trade_date",
        "required_open",
        "required_high",
        "required_low",
        "required_close",
        "required_volume",
    }


def test_bist_price_non_positive_errors() -> None:
    results = bist_daily_ohlcv._price_positive({"open": 0, "high": 11, "low": -1, "close": 10})
    names = {r.rule_name for r in results}
    assert names == {"open_non_positive", "low_non_positive"}
    assert all(r.severity is Severity.ERROR for r in results)


def test_bist_price_positive_silent() -> None:
    assert bist_daily_ohlcv._price_positive({"open": 1, "high": 2, "low": 0.5, "close": 1.5}) == []


def test_bist_volume_zero_warns() -> None:
    results = bist_daily_ohlcv._volume_range({"volume": 0})
    assert len(results) == 1
    assert results[0].rule_name == "volume_zero"
    assert results[0].severity is Severity.WARN


def test_bist_volume_negative_errors() -> None:
    results = bist_daily_ohlcv._volume_range({"volume": -5})
    assert len(results) == 1
    assert results[0].rule_name == "volume_negative"
    assert results[0].severity is Severity.ERROR


def test_bist_volume_positive_silent() -> None:
    assert bist_daily_ohlcv._volume_range({"volume": 1}) == []
    assert bist_daily_ohlcv._volume_range({}) == []


def test_bist_ohlc_cross_field_errors() -> None:
    # low > high
    results = bist_daily_ohlcv._ohlc_cross_field({"open": 10, "high": 5, "low": 8, "close": 6})
    names = {r.rule_name for r in results}
    # low_above_high, open_above_high, open_below_low? Let's pick a tighter case.
    assert "low_above_high" in names

    # open above high; close below low
    results = bist_daily_ohlcv._ohlc_cross_field({"open": 15, "high": 11, "low": 9, "close": 8})
    names = {r.rule_name for r in results}
    assert "open_above_high" in names
    assert "close_below_low" in names


def test_bist_ohlc_cross_field_silent_when_consistent() -> None:
    assert (
        bist_daily_ohlcv._ohlc_cross_field({"open": 10, "high": 11, "low": 9, "close": 10.5}) == []
    )


def test_bist_ohlc_cross_field_partial_record_silent() -> None:
    """Cross-field rules must NOT fire when one side is missing."""
    assert bist_daily_ohlcv._ohlc_cross_field({"open": 10}) == []
    assert bist_daily_ohlcv._ohlc_cross_field({"high": 5, "low": 8}) != []


# ---------------------------------------------------------------------------
# tefas.fund_holding
# ---------------------------------------------------------------------------


def test_tefas_happy_path_silent() -> None:
    record = {
        "fund_id": 1,
        "entity_id": 2,
        "snapshot_date": "2026-01-01",
        "quantity": 1000,
    }
    assert _run_all(tefas_fund_holding, record) == []


def test_tefas_required_fields_fire() -> None:
    results = tefas_fund_holding._required_fields({})
    names = {r.rule_name for r in results}
    assert names == {
        "required_fund_id",
        "required_entity_id",
        "required_snapshot_date",
        "required_quantity",
    }


def test_tefas_quantity_negative_errors() -> None:
    results = tefas_fund_holding._quantity_non_negative({"quantity": -1})
    assert len(results) == 1
    assert results[0].rule_name == "quantity_negative"
    assert results[0].severity is Severity.ERROR


def test_tefas_quantity_non_negative_silent() -> None:
    assert tefas_fund_holding._quantity_non_negative({"quantity": 0}) == []
    assert tefas_fund_holding._quantity_non_negative({"quantity": 100}) == []
    assert tefas_fund_holding._quantity_non_negative({}) == []


# ---------------------------------------------------------------------------
# mkk.capital_action
# ---------------------------------------------------------------------------


def test_mkk_happy_path_silent() -> None:
    record = {
        "entity_id": 1,
        "event_at": "2026-01-01T00:00:00Z",
        "action_type": "dividend",
    }
    assert _run_all(mkk_capital_action, record) == []


def test_mkk_required_fields_fire() -> None:
    results = mkk_capital_action._required_fields({})
    names = {r.rule_name for r in results}
    assert names == {
        "required_entity_id",
        "required_event_at",
        "required_action_type",
    }


def test_mkk_event_at_future_errors() -> None:
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    results = mkk_capital_action._event_at_temporal({"event_at": future})
    assert len(results) == 1
    assert results[0].rule_name == "event_at_in_future"


def test_mkk_event_at_past_silent() -> None:
    past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    assert mkk_capital_action._event_at_temporal({"event_at": past}) == []
    assert mkk_capital_action._event_at_temporal({}) == []


# ---------------------------------------------------------------------------
# Default registry shape
# ---------------------------------------------------------------------------


def test_default_rules_registry_has_seven_tables() -> None:
    """All 7 M0 tables must be wired into _DEFAULT_RULES."""
    from aslan_core.dq import validation

    assert set(validation._DEFAULT_RULES.keys()) == {
        "ts.canonical_financial",
        "ts.financial_line_item",
        "kap.disclosures",
        "evds.observation",
        "bist.daily_ohlcv",
        "tefas.fund_holding",
        "mkk.capital_action",
    }
    # Each table has at least one rule.
    for table, rules in validation._DEFAULT_RULES.items():
        assert len(rules) >= 1, f"{table} has no rules"


def test_pk_fields_registry_has_seven_tables() -> None:
    from aslan_core.dq import validation

    assert set(validation._PK_FIELDS_BY_TABLE.keys()) == {
        "ts.canonical_financial",
        "ts.financial_line_item",
        "kap.disclosures",
        "evds.observation",
        "bist.daily_ohlcv",
        "tefas.fund_holding",
        "mkk.capital_action",
    }
