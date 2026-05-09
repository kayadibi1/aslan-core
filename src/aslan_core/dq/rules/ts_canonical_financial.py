"""Validation rules for ts.canonical_financial.

Per spec Appendix B:
- required(entity_id, period_end, as_of)
- range(revenue >= 0)
- range(-1e15 < net_income < 1e15)
- cross-field-warn(net_income <= revenue)
- temporal(period_end <= now())
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aslan_core.dq.rules._types import Rule, RuleResult
from aslan_core.dq.types import Severity


def _required_fields(record: dict[str, Any]) -> list[RuleResult]:
    out: list[RuleResult] = []
    for f in ("entity_id", "period_end", "as_of"):
        if record.get(f) is None:
            out.append(
                RuleResult(
                    rule_name=f"required_{f}",
                    severity=Severity.ERROR,
                    detail={"missing_field": f},
                )
            )
    return out


def _revenue_range(record: dict[str, Any]) -> list[RuleResult]:
    rev = record.get("revenue")
    if rev is not None and rev < 0:
        return [
            RuleResult(
                rule_name="revenue_non_negative",
                severity=Severity.ERROR,
                detail={"revenue": rev},
            )
        ]
    return []


def _net_income_range(record: dict[str, Any]) -> list[RuleResult]:
    ni = record.get("net_income")
    if ni is not None and not (-1e15 < ni < 1e15):
        return [
            RuleResult(
                rule_name="net_income_range",
                severity=Severity.ERROR,
                detail={"net_income": ni},
            )
        ]
    return []


def _net_income_le_revenue(record: dict[str, Any]) -> list[RuleResult]:
    rev = record.get("revenue")
    ni = record.get("net_income")
    if rev is not None and ni is not None and ni > rev:
        return [
            RuleResult(
                rule_name="net_income_exceeds_revenue",
                severity=Severity.WARN,  # spec: warn, not block (TR FX gains)
                detail={"revenue": rev, "net_income": ni},
            )
        ]
    return []


def _period_end_temporal(record: dict[str, Any]) -> list[RuleResult]:
    pe = record.get("period_end")
    if pe is None:
        return []
    pe_dt = datetime.fromisoformat(pe.replace("Z", "+00:00")) if isinstance(pe, str) else pe
    now_utc = datetime.now(UTC)
    if pe_dt.tzinfo is None:
        pe_dt = pe_dt.replace(tzinfo=UTC)
    if pe_dt > now_utc:
        return [
            RuleResult(
                rule_name="period_end_in_future",
                severity=Severity.ERROR,
                detail={"period_end": str(pe), "now_utc": now_utc.isoformat()},
            )
        ]
    return []


RULES: list[Rule] = [
    Rule(name="required_fields", run=_required_fields),
    Rule(name="revenue_range", run=_revenue_range),
    Rule(name="net_income_range", run=_net_income_range),
    Rule(name="net_income_le_revenue", run=_net_income_le_revenue),
    Rule(name="period_end_temporal", run=_period_end_temporal),
]
