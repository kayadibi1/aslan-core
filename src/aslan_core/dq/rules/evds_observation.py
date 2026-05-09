"""Validation rules for evds.observation.

Per spec Appendix B:
- required(series_code, observation_date, value)
- temporal(observation_date <= now())
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from aslan_core.dq.rules._types import Rule, RuleResult
from aslan_core.dq.types import Severity


def _required_fields(record: dict[str, Any]) -> list[RuleResult]:
    out: list[RuleResult] = []
    for f in ("series_code", "observation_date", "value"):
        if record.get(f) is None:
            out.append(
                RuleResult(
                    rule_name=f"required_{f}",
                    severity=Severity.ERROR,
                    detail={"missing_field": f},
                )
            )
    return out


def _observation_date_temporal(record: dict[str, Any]) -> list[RuleResult]:
    od = record.get("observation_date")
    if od is None:
        return []
    if isinstance(od, str):
        # Accept either YYYY-MM-DD or full ISO-8601 timestamp.
        od_dt: datetime = datetime.fromisoformat(od.replace("Z", "+00:00"))
    elif isinstance(od, datetime):
        od_dt = od
    elif isinstance(od, date):
        od_dt = datetime(od.year, od.month, od.day, tzinfo=UTC)
    else:
        return []
    now_utc = datetime.now(UTC)
    if od_dt.tzinfo is None:
        od_dt = od_dt.replace(tzinfo=UTC)
    if od_dt > now_utc:
        return [
            RuleResult(
                rule_name="observation_date_in_future",
                severity=Severity.ERROR,
                detail={"observation_date": str(od), "now_utc": now_utc.isoformat()},
            )
        ]
    return []


RULES: list[Rule] = [
    Rule(name="required_fields", run=_required_fields),
    Rule(name="observation_date_temporal", run=_observation_date_temporal),
]
