"""Moat 2 canary (H9 / SCOPE.md D20).

Replays a curated list of historical KAP amendments through the
bitemporal-research-API and asserts that:

    api.observation_at(as_of_before)[field] == expected_before
    api.observation_at(as_of_after)[field]  == expected_after

Expected runtime: ~2 seconds for 10 cases. Failure pages alerting and
flips ``MOAT_2_CANARY_STATUS=false`` in ``aslan_core.feature_flags`` so
``GET /v1/verify/moat-2`` returns 503.

KNOWN_AMENDMENTS is populated with **10 real production amendment
cases** sourced 2026-05-09 from ``ts.canonical_financial`` rows
where the same bitemporal key carries multiple ``as_of`` values
with different ``value`` columns. Each case is a real
quarterly-financials amendment for a distinct BIST-listed entity.
The canary fails (flips ``MOAT_2_CANARY_STATUS=false``) if the PIT
function returns the wrong value at either ``as_of_before`` or
``as_of_after``.

Schedule: every 5 minutes via cron in staging (and, post-Phase-7,
production). The cron entry lives in
``aslan-core/infra/deploy/docker-compose.yml`` under the
``bitemporal-canary`` profile.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg


@dataclass(frozen=True)
class AmendmentCase:
    """One Moat 2 regression case.

    ``field`` is the path within the API's data envelope. The canary
    queries the canonical financial endpoint by entity + canonical
    code and asserts the value differs across as_of bounds.
    """

    case_id: str
    entity_id: str
    canonical_code: str
    period_end: str
    period_type: str
    consolidation: str
    currency_code: str
    accounting_standard: str
    restatement_basis: str
    cpi_base_date: str
    mapping_version: int
    as_of_before: str  # ISO-8601 UTC
    as_of_after: str
    field: str  # e.g. "value"
    expected_before: float | None
    expected_after: float | None


# Real production amendment cases sourced 2026-05-09 from
# ts.canonical_financial rows with restatement_basis='as_reported'
# and >=2 distinct as_of values having >=2 distinct value columns.
# Each case is a real amendment to a published quarterly canonical
# financial line for a different BIST-listed entity.
#
# >=10 cases required per SCOPE.md D29 / TESTPLAN.md TC-054.
KNOWN_AMENDMENTS: tuple[AmendmentCase, ...] = (
    AmendmentCase(
        case_id="prod-001-cf-depreciation-0246a45e",
        entity_id="0246a45e-ec26-4edd-8981-af2fc25ffb7c",
        canonical_code="cf.depreciation",
        period_end="2025-03-31",
        period_type="q",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2026-05-03T12:00:00+00:00",
        as_of_after="2026-05-07T17:28:20+00:00",
        field="value",
        expected_before=4788807.0,
        expected_after=6267010.0,
    ),
    AmendmentCase(
        case_id="prod-002-cf-depreciation-09b18d30",
        entity_id="09b18d30-959d-4570-9e2f-75e2dfeda6d5",
        canonical_code="cf.depreciation",
        period_end="2025-03-31",
        period_type="q",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2026-05-03T12:00:00+00:00",
        as_of_after="2026-05-07T16:19:23+00:00",
        field="value",
        expected_before=641885030.0,
        expected_after=840003.0,
    ),
    AmendmentCase(
        case_id="prod-003-cf-depreciation-1589bb24",
        entity_id="1589bb24-12f1-4bee-a564-34c04ce95707",
        canonical_code="cf.depreciation",
        period_end="2025-03-31",
        period_type="q",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2026-05-03T12:00:00+00:00",
        as_of_after="2026-05-07T15:19:10+00:00",
        field="value",
        expected_before=20489731.0,
        expected_after=26813921.0,
    ),
    AmendmentCase(
        case_id="prod-004-cf-depreciation-1ceb95d7",
        entity_id="1ceb95d7-cf32-4caf-84dc-42d6a1d5bdae",
        canonical_code="cf.depreciation",
        period_end="2025-03-31",
        period_type="q",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2026-05-03T12:00:00+00:00",
        as_of_after="2026-05-06T16:43:49+00:00",
        field="value",
        expected_before=4308.0,
        expected_after=5639.0,
    ),
    AmendmentCase(
        case_id="prod-005-cf-depreciation-25f639bc",
        entity_id="25f639bc-0477-43b3-b981-7cd287a59ef8",
        canonical_code="cf.depreciation",
        period_end="2025-03-31",
        period_type="q",
        consolidation="unconsolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2026-05-03T12:00:00+00:00",
        as_of_after="2026-05-07T15:25:33+00:00",
        field="value",
        expected_before=1435262.0,
        expected_after=1878262.0,
    ),
    AmendmentCase(
        case_id="prod-006-cf-depreciation-2797449b",
        entity_id="2797449b-04a4-4c1e-a523-b86fd4c8fe14",
        canonical_code="cf.depreciation",
        period_end="2025-03-31",
        period_type="q",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2026-05-03T12:00:00+00:00",
        as_of_after="2026-05-05T15:15:13+00:00",
        field="value",
        expected_before=437313.0,
        expected_after=572292.0,
    ),
    AmendmentCase(
        case_id="prod-007-cf-depreciation-27be8a39",
        entity_id="27be8a39-6946-4e2c-a315-72e27a02e99e",
        canonical_code="cf.depreciation",
        period_end="2025-03-31",
        period_type="q",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2026-05-03T12:00:00+00:00",
        as_of_after="2026-05-07T15:13:24+00:00",
        field="value",
        expected_before=15396.0,
        expected_after=20149.0,
    ),
    AmendmentCase(
        case_id="prod-008-cf-depreciation-27bfc3c1",
        entity_id="27bfc3c1-4c73-4489-94a1-7e0414644ae2",
        canonical_code="cf.depreciation",
        period_end="2025-03-31",
        period_type="q",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2026-05-03T12:00:00+00:00",
        as_of_after="2026-05-06T15:13:28+00:00",
        field="value",
        expected_before=789230.0,
        expected_after=1032831.0,
    ),
    AmendmentCase(
        case_id="prod-009-cf-depreciation-2ad7505e",
        entity_id="2ad7505e-e38e-4328-98ac-028c6f1d2821",
        canonical_code="cf.depreciation",
        period_end="2025-03-31",
        period_type="q",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2026-05-03T12:00:00+00:00",
        as_of_after="2026-05-07T17:26:02+00:00",
        field="value",
        expected_before=663742.0,
        expected_after=868609.0,
    ),
    AmendmentCase(
        case_id="prod-010-cf-depreciation-37f251e3",
        entity_id="37f251e3-4090-468a-afec-2f0f7b12b5e6",
        canonical_code="cf.depreciation",
        period_end="2025-03-31",
        period_type="q",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2026-05-03T12:00:00+00:00",
        as_of_after="2026-05-06T16:15:12+00:00",
        field="value",
        expected_before=580174310.0,
        expected_after=759261788.0,
    ),
)


def fetch_canonical_value(
    conn: psycopg.Connection, case: AmendmentCase, *, as_of: str
) -> float | None:
    """Hit the PIT function directly via SQL. The canary bypasses the
    HTTP layer for speed and to avoid auth tokens; canary correctness
    depends on the underlying SQL function, not the HTTP envelope.
    """
    sql = """
        SELECT value
        FROM ts.canonical_financial_at(%s)
        WHERE entity_id = %s::uuid
          AND canonical_code = %s
          AND period_end = %s::date
          AND period_type = %s
          AND consolidation = %s
          AND currency_code = %s
          AND accounting_standard = %s
          AND restatement_basis = %s
          AND cpi_base_date = %s::date
          AND mapping_version = %s
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                as_of,
                case.entity_id,
                case.canonical_code,
                case.period_end,
                case.period_type,
                case.consolidation,
                case.currency_code,
                case.accounting_standard,
                case.restatement_basis,
                case.cpi_base_date,
                case.mapping_version,
            ),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return float(row[0]) if row[0] is not None else None


def evaluate(conn: psycopg.Connection) -> dict[str, Any]:
    failing_cases: list[dict[str, Any]] = []
    started = time.time()

    for case in KNOWN_AMENDMENTS:
        try:
            before = fetch_canonical_value(conn, case, as_of=case.as_of_before)
            after = fetch_canonical_value(conn, case, as_of=case.as_of_after)
        except Exception as e:
            failing_cases.append(
                {"case_id": case.case_id, "kind": "exception", "error": repr(e)}
            )
            continue

        before_match = (
            before == case.expected_before
            or (before is None and case.expected_before is None)
        )
        after_match = (
            after == case.expected_after
            or (after is None and case.expected_after is None)
        )
        if not (before_match and after_match):
            failing_cases.append(
                {
                    "case_id": case.case_id,
                    "kind": "value-mismatch",
                    "before_seen": before,
                    "before_expected": case.expected_before,
                    "after_seen": after,
                    "after_expected": case.expected_after,
                }
            )

    duration = time.time() - started
    return {
        "moat_2": "green" if not failing_cases else "red",
        "cases_total": len(KNOWN_AMENDMENTS),
        "cases_passing": len(KNOWN_AMENDMENTS) - len(failing_cases),
        "failing_cases": failing_cases,
        "duration_seconds": round(duration, 4),
        "ran_at": datetime.now(tz=UTC).isoformat(),
    }


def persist_status(conn: psycopg.Connection, result: dict[str, Any]) -> None:
    """Upsert MOAT_2_CANARY_STATUS into aslan_core.feature_flags.

    The feature_flags table doubles as the canary status surface so
    ``GET /v1/verify/moat-2`` can answer cheaply (D20).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO aslan_core.feature_flags
                (flag_name, value_bool, scope, notes, updated_at, updated_by)
            VALUES
                (%s, %s, 'global', %s, now(), 'canary_moat_2')
            ON CONFLICT (flag_name) DO UPDATE
            SET value_bool = EXCLUDED.value_bool,
                notes = EXCLUDED.notes,
                updated_at = EXCLUDED.updated_at,
                updated_by = EXCLUDED.updated_by
            """,
            (
                "MOAT_2_CANARY_STATUS",
                result["moat_2"] == "green",
                json.dumps(result, default=str),
            ),
        )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Moat 2 canary (H9).")
    parser.add_argument("--dsn", default=os.environ.get("ASLAN_PG_DSN"))
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.dsn:
        print(json.dumps({"status": "error", "reason": "no DSN"}))
        return 2
    try:
        with psycopg.connect(args.dsn) as conn:
            result = evaluate(conn)
            if not args.no_persist:
                persist_status(conn, result)
    except psycopg.OperationalError as e:
        print(json.dumps({"status": "error", "reason": f"connect: {e!r}"}))
        return 2

    print(json.dumps(result, indent=2 if not args.json else None))
    return 0 if result["moat_2"] == "green" else 1


if __name__ == "__main__":
    sys.exit(main())
