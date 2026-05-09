"""Validation rules for ts.financial_line_item.

Per spec Appendix B:
- required(canonical_financial_id, line_item, value)
- range(value plausibility per line_item) — for M0 we just check that
  `value` is numeric when present; per-line-item plausibility ranges
  land in M1.
"""

from __future__ import annotations

from numbers import Real
from typing import Any

from aslan_core.dq.rules._types import Rule, RuleResult
from aslan_core.dq.types import Severity


def _required_fields(record: dict[str, Any]) -> list[RuleResult]:
    out: list[RuleResult] = []
    for f in ("canonical_financial_id", "line_item", "value"):
        if record.get(f) is None:
            out.append(
                RuleResult(
                    rule_name=f"required_{f}",
                    severity=Severity.ERROR,
                    detail={"missing_field": f},
                )
            )
    return out


def _value_numeric(record: dict[str, Any]) -> list[RuleResult]:
    """`value` (when present) must be a real number.

    M0 only catches the obvious type mismatch; per-line-item
    plausibility ranges (e.g. revenue >= 0, EPS bounds) land in M1.
    bool is a subclass of int — exclude it explicitly so True/False
    in `value` still flags.
    """
    value = record.get("value")
    if value is None:
        return []
    if isinstance(value, bool) or not isinstance(value, Real):
        return [
            RuleResult(
                rule_name="value_not_numeric",
                severity=Severity.ERROR,
                detail={"value": str(value), "type": type(value).__name__},
            )
        ]
    return []


RULES: list[Rule] = [
    Rule(name="required_fields", run=_required_fields),
    Rule(name="value_numeric", run=_value_numeric),
]
