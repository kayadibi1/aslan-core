"""dq.validation — in-line per-record validation.

Caller pattern:

    failures = await validation.check(
        session=s,
        source="kap",
        table="ts.canonical_financial",
        record=record,
    )
    # failures is List[ValidationFailure]; each one is already
    # persisted to audit.validation_failure by this call. Caller
    # decides whether to also quarantine the record (audit does
    # not gate writes).

Rules are looked up by table name from a default registry under
`aslan_core.dq.rules.<table_module>`. The registry is populated for
all 7 M0 tables per spec Appendix B; tests can override via the
`rules=` kwarg.

`RuleResult` is the per-firing wire shape returned by individual rule
callables before persistence. It lives in `aslan_core.dq.rules._types`
to keep the rules package importable without going through this
module; we re-export it here for callers who only know
`aslan_core.dq.validation`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.dq._sql import INSERT_VALIDATION_FAILURE
from aslan_core.dq.rules import (
    Rule,
    RuleResult,
    bist_daily_ohlcv,
    evds_observation,
    kap_disclosures,
    mkk_capital_action,
    tefas_fund_holding,
    ts_canonical_financial,
    ts_financial_line_item,
)
from aslan_core.dq.types import ValidationFailure

__all__ = ["RuleResult", "check"]


# Per-table PK extraction. Matches the canonical PK columns of each
# target table, so persisted `record_pk` JSON is queryable by the
# same shape readers expect.
_PK_FIELDS_BY_TABLE: dict[str, tuple[str, ...]] = {
    "ts.canonical_financial": ("entity_id", "period_end", "as_of"),
    "ts.financial_line_item": ("canonical_financial_id", "line_item"),
    "kap.disclosures": ("disclosure_id",),
    "evds.observation": ("series_code", "observation_date"),
    "bist.daily_ohlcv": ("security_id", "trade_date"),
    "tefas.fund_holding": ("fund_id", "entity_id", "snapshot_date"),
    "mkk.capital_action": ("entity_id", "event_at"),
}


_DEFAULT_RULES: dict[str, list[Rule]] = {
    "ts.canonical_financial": ts_canonical_financial.RULES,
    "ts.financial_line_item": ts_financial_line_item.RULES,
    "kap.disclosures": kap_disclosures.RULES,
    "evds.observation": evds_observation.RULES,
    "bist.daily_ohlcv": bist_daily_ohlcv.RULES,
    "tefas.fund_holding": tefas_fund_holding.RULES,
    "mkk.capital_action": mkk_capital_action.RULES,
}


def _extract_pk(table: str, record: dict[str, Any]) -> dict[str, Any]:
    fields = _PK_FIELDS_BY_TABLE.get(table)
    if fields is None:
        return {}
    return {f: record.get(f) for f in fields}


async def check(
    *,
    session: AsyncSession,
    source: str,
    table: str,
    record: dict[str, Any],
    rules: Mapping[str, list[Rule]] | None = None,
) -> list[ValidationFailure]:
    """Run all registered rules for `table` against `record`.

    Persists one row to `audit.validation_failure` per rule that
    fires, then returns the list (with `failure_id` populated) so
    the caller can branch on the result.

    `rules` defaults to `_DEFAULT_RULES`; tests pass an override map.
    """
    table_rules = (rules if rules is not None else _DEFAULT_RULES).get(table, [])
    if not table_rules:
        return []

    pk = _extract_pk(table, record)
    detected_at_dt = datetime.now(UTC)
    detected_at_iso = detected_at_dt.isoformat()
    failures: list[ValidationFailure] = []

    for rule in table_rules:
        results = rule.run(record)
        for result in results:
            row = await session.execute(
                INSERT_VALIDATION_FAILURE,
                {
                    "source": source,
                    "rule_name": result.rule_name,
                    "severity": result.severity.value,
                    "record_table": table,
                    "record_pk": json.dumps(pk, default=str),
                    # asyncpg's TIMESTAMPTZ codec wants a tz-aware
                    # datetime, not an ISO string.
                    "detected_at": detected_at_dt,
                    "detail": json.dumps(result.detail, default=str),
                },
            )
            failure_id, _recorded_at = row.one()
            failures.append(
                ValidationFailure(
                    source=source,
                    rule_name=result.rule_name,
                    severity=result.severity,
                    record_table=table,
                    record_pk=pk,
                    detail=result.detail,
                    # Public dataclass keeps the ISO string for
                    # downstream JSON serialization simplicity.
                    detected_at=detected_at_iso,
                    failure_id=int(failure_id),
                )
            )
    return failures
