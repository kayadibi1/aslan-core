"""Validation rules for kap.disclosures.

Per spec Appendix B:
- required(disclosure_id, entity_id, published_at)
- temporal(published_at <= now())
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aslan_core.dq.rules._types import Rule, RuleResult
from aslan_core.dq.types import Severity


def _required_fields(record: dict[str, Any]) -> list[RuleResult]:
    out: list[RuleResult] = []
    for f in ("disclosure_id", "entity_id", "published_at"):
        if record.get(f) is None:
            out.append(
                RuleResult(
                    rule_name=f"required_{f}",
                    severity=Severity.ERROR,
                    detail={"missing_field": f},
                )
            )
    return out


def _published_at_temporal(record: dict[str, Any]) -> list[RuleResult]:
    pa = record.get("published_at")
    if pa is None:
        return []
    pa_dt = datetime.fromisoformat(pa.replace("Z", "+00:00")) if isinstance(pa, str) else pa
    now_utc = datetime.now(UTC)
    if pa_dt.tzinfo is None:
        pa_dt = pa_dt.replace(tzinfo=UTC)
    if pa_dt > now_utc:
        return [
            RuleResult(
                rule_name="published_at_in_future",
                severity=Severity.ERROR,
                detail={"published_at": str(pa), "now_utc": now_utc.isoformat()},
            )
        ]
    return []


RULES: list[Rule] = [
    Rule(name="required_fields", run=_required_fields),
    Rule(name="published_at_temporal", run=_published_at_temporal),
]
