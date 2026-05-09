"""Validation rules for bist.daily_ohlcv.

Per spec Appendix B:
- required(security_id, trade_date, open, high, low, close, volume)
- range: open/high/low/close > 0 (ERROR if any <= 0)
- range: volume >= 0; volume == 0 is WARN (some thin securities have
  legit zero-volume days, but it is worth flagging)
- cross-field: low <= open <= high; low <= close <= high
"""

from __future__ import annotations

from typing import Any

from aslan_core.dq.rules._types import Rule, RuleResult
from aslan_core.dq.types import Severity


def _required_fields(record: dict[str, Any]) -> list[RuleResult]:
    out: list[RuleResult] = []
    for f in ("security_id", "trade_date", "open", "high", "low", "close", "volume"):
        if record.get(f) is None:
            out.append(
                RuleResult(
                    rule_name=f"required_{f}",
                    severity=Severity.ERROR,
                    detail={"missing_field": f},
                )
            )
    return out


def _price_positive(record: dict[str, Any]) -> list[RuleResult]:
    out: list[RuleResult] = []
    for f in ("open", "high", "low", "close"):
        v = record.get(f)
        if v is not None and v <= 0:
            out.append(
                RuleResult(
                    rule_name=f"{f}_non_positive",
                    severity=Severity.ERROR,
                    detail={f: v},
                )
            )
    return out


def _volume_range(record: dict[str, Any]) -> list[RuleResult]:
    v = record.get("volume")
    if v is None:
        return []
    if v < 0:
        return [
            RuleResult(
                rule_name="volume_negative",
                severity=Severity.ERROR,
                detail={"volume": v},
            )
        ]
    if v == 0:
        return [
            RuleResult(
                rule_name="volume_zero",
                severity=Severity.WARN,
                detail={"volume": v},
            )
        ]
    return []


def _ohlc_cross_field(record: dict[str, Any]) -> list[RuleResult]:
    out: list[RuleResult] = []
    o = record.get("open")
    h = record.get("high")
    low = record.get("low")
    c = record.get("close")
    if low is not None and h is not None and low > h:
        out.append(
            RuleResult(
                rule_name="low_above_high",
                severity=Severity.ERROR,
                detail={"low": low, "high": h},
            )
        )
    if o is not None and h is not None and o > h:
        out.append(
            RuleResult(
                rule_name="open_above_high",
                severity=Severity.ERROR,
                detail={"open": o, "high": h},
            )
        )
    if o is not None and low is not None and o < low:
        out.append(
            RuleResult(
                rule_name="open_below_low",
                severity=Severity.ERROR,
                detail={"open": o, "low": low},
            )
        )
    if c is not None and h is not None and c > h:
        out.append(
            RuleResult(
                rule_name="close_above_high",
                severity=Severity.ERROR,
                detail={"close": c, "high": h},
            )
        )
    if c is not None and low is not None and c < low:
        out.append(
            RuleResult(
                rule_name="close_below_low",
                severity=Severity.ERROR,
                detail={"close": c, "low": low},
            )
        )
    return out


RULES: list[Rule] = [
    Rule(name="required_fields", run=_required_fields),
    Rule(name="price_positive", run=_price_positive),
    Rule(name="volume_range", run=_volume_range),
    Rule(name="ohlc_cross_field", run=_ohlc_cross_field),
]
