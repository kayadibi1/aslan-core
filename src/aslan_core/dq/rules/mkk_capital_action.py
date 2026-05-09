"""Validation rules for mkk.capital_action.

Per spec Appendix B:
- required(entity_id, event_at, action_type)
- temporal(event_at <= now())
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aslan_core.dq.rules._types import Rule, RuleResult
from aslan_core.dq.types import Severity


def _required_fields(record: dict[str, Any]) -> list[RuleResult]:
    out: list[RuleResult] = []
    for f in ("entity_id", "event_at", "action_type"):
        if record.get(f) is None:
            out.append(
                RuleResult(
                    rule_name=f"required_{f}",
                    severity=Severity.ERROR,
                    detail={"missing_field": f},
                )
            )
    return out


def _event_at_temporal(record: dict[str, Any]) -> list[RuleResult]:
    ea = record.get("event_at")
    if ea is None:
        return []
    ea_dt = datetime.fromisoformat(ea.replace("Z", "+00:00")) if isinstance(ea, str) else ea
    now_utc = datetime.now(UTC)
    if ea_dt.tzinfo is None:
        ea_dt = ea_dt.replace(tzinfo=UTC)
    if ea_dt > now_utc:
        return [
            RuleResult(
                rule_name="event_at_in_future",
                severity=Severity.ERROR,
                detail={"event_at": str(ea), "now_utc": now_utc.isoformat()},
            )
        ]
    return []


RULES: list[Rule] = [
    Rule(name="required_fields", run=_required_fields),
    Rule(name="event_at_temporal", run=_event_at_temporal),
]
