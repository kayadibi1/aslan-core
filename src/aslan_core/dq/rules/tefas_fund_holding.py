"""Validation rules for tefas.fund_holding.

Per spec Appendix B:
- required(fund_id, entity_id, snapshot_date, quantity)
- range(quantity >= 0)
"""

from __future__ import annotations

from typing import Any

from aslan_core.dq.rules._types import Rule, RuleResult
from aslan_core.dq.types import Severity


def _required_fields(record: dict[str, Any]) -> list[RuleResult]:
    out: list[RuleResult] = []
    for f in ("fund_id", "entity_id", "snapshot_date", "quantity"):
        if record.get(f) is None:
            out.append(
                RuleResult(
                    rule_name=f"required_{f}",
                    severity=Severity.ERROR,
                    detail={"missing_field": f},
                )
            )
    return out


def _quantity_non_negative(record: dict[str, Any]) -> list[RuleResult]:
    q = record.get("quantity")
    if q is not None and q < 0:
        return [
            RuleResult(
                rule_name="quantity_negative",
                severity=Severity.ERROR,
                detail={"quantity": q},
            )
        ]
    return []


RULES: list[Rule] = [
    Rule(name="required_fields", run=_required_fields),
    Rule(name="quantity_non_negative", run=_quantity_non_negative),
]
