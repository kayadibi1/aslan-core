"""Per-table validation rule registry.

`Rule`, `RuleFn`, and `RuleResult` are the public protocol shared
across the `dq.validation` runner and per-table rule modules. The
per-table modules live as siblings here and each exposes a
`RULES: list[Rule]` constant consumed by `dq.validation._DEFAULT_RULES`.
"""

from __future__ import annotations

from aslan_core.dq.rules import (
    bist_daily_ohlcv,
    evds_observation,
    kap_disclosures,
    mkk_capital_action,
    tefas_fund_holding,
    ts_canonical_financial,
    ts_financial_line_item,
)
from aslan_core.dq.rules._types import Rule, RuleFn, RuleResult

__all__ = [
    "Rule",
    "RuleFn",
    "RuleResult",
    "bist_daily_ohlcv",
    "evds_observation",
    "kap_disclosures",
    "mkk_capital_action",
    "tefas_fund_holding",
    "ts_canonical_financial",
    "ts_financial_line_item",
]
