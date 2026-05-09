"""Moat 2 canary (H9 / SCOPE.md D20).

Replays a curated list of historical KAP amendments through the
bitemporal-research-API and asserts that:

    api.observation_at(as_of_before)[field] == expected_before
    api.observation_at(as_of_after)[field]  == expected_after

Expected runtime: ~2 seconds for 10 cases. Failure pages alerting and
flips ``MOAT_2_CANARY_STATUS=false`` in ``aslan_core.feature_flags`` so
``GET /v1/verify/moat-2`` returns 503.

Phase 4 skeleton: KNOWN_AMENDMENTS is a placeholder; the full
hand-curated set of ≥10 cases is populated as
``aslan-event-extractor`` lands its M1 + a backfill of
``agg.filing_event`` rows. Until then the canary runs against a
synthetic test fixture seeded into staging by the integration test
suite.

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


# SYNTHETIC v1 fixtures — populated as a placeholder set so the canary
# loop, the /verify/moat-2 plumbing, and the integration tests have
# something to chew on. Per HANDOFF.md "Populate KNOWN_AMENDMENTS from
# production after Phase 7e": the next session must replace these
# entity_ids with real production UUIDs (sourced from
# ``ts.canonical_financial`` rows that have ≥2 distinct ``as_of`` per
# bitemporal key) and verify each before/after value against the
# corresponding KAP filing.
#
# Until that backfill happens, the staging canary is seeded with these
# rows by the integration test suite (``tests/research/``) before
# ``scripts/canary_moat_2.py --once`` runs. The production canary
# stays red ("unknown — no curated amendments yet") until the swap.
#
# ≥10 cases required per SCOPE.md D29 / TESTPLAN.md TC-054.
KNOWN_AMENDMENTS: tuple[AmendmentCase, ...] = (
    AmendmentCase(
        case_id="synthetic-001-revenue-amend",
        entity_id="00000000-0000-0000-0000-000000000001",
        canonical_code="revenue",
        period_end="2024-03-31",
        period_type="q",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2024-04-25T10:00:00+00:00",
        as_of_after="2024-05-10T16:00:00+00:00",
        field="value",
        expected_before=1_250_000_000.0,
        expected_after=1_287_450_000.0,
    ),
    AmendmentCase(
        case_id="synthetic-002-gross-profit-amend",
        entity_id="00000000-0000-0000-0000-000000000002",
        canonical_code="gross_profit",
        period_end="2024-06-30",
        period_type="q",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2024-07-22T11:30:00+00:00",
        as_of_after="2024-08-05T14:15:00+00:00",
        field="value",
        expected_before=420_000_000.0,
        expected_after=415_500_000.0,
    ),
    AmendmentCase(
        case_id="synthetic-003-operating-income-correction",
        entity_id="00000000-0000-0000-0000-000000000003",
        canonical_code="operating_income",
        period_end="2024-09-30",
        period_type="q",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2024-10-28T09:45:00+00:00",
        as_of_after="2024-11-15T17:00:00+00:00",
        field="value",
        expected_before=315_750_000.0,
        expected_after=308_900_000.0,
    ),
    AmendmentCase(
        case_id="synthetic-004-net-income-amend",
        entity_id="00000000-0000-0000-0000-000000000004",
        canonical_code="net_income",
        period_end="2024-12-31",
        period_type="y",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2025-03-15T10:00:00+00:00",
        as_of_after="2025-04-20T16:00:00+00:00",
        field="value",
        expected_before=895_300_000.0,
        expected_after=902_150_000.0,
    ),
    AmendmentCase(
        case_id="synthetic-005-total-assets-amend",
        entity_id="00000000-0000-0000-0000-000000000005",
        canonical_code="total_assets",
        period_end="2024-12-31",
        period_type="y",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2025-03-20T08:00:00+00:00",
        as_of_after="2025-04-10T11:30:00+00:00",
        field="value",
        expected_before=12_400_000_000.0,
        expected_after=12_385_000_000.0,
    ),
    AmendmentCase(
        case_id="synthetic-006-total-equity-correction",
        entity_id="00000000-0000-0000-0000-000000000006",
        canonical_code="total_equity",
        period_end="2024-12-31",
        period_type="y",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2025-03-25T13:00:00+00:00",
        as_of_after="2025-04-12T15:00:00+00:00",
        field="value",
        expected_before=4_750_000_000.0,
        expected_after=4_762_300_000.0,
    ),
    AmendmentCase(
        case_id="synthetic-007-eps-basic-amend",
        entity_id="00000000-0000-0000-0000-000000000007",
        canonical_code="eps_basic",
        period_end="2024-12-31",
        period_type="y",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2025-03-15T10:00:00+00:00",
        as_of_after="2025-04-20T16:00:00+00:00",
        field="value",
        expected_before=2.45,
        expected_after=2.51,
    ),
    AmendmentCase(
        case_id="synthetic-008-dividends-declared-amend",
        entity_id="00000000-0000-0000-0000-000000000008",
        canonical_code="dividends_declared",
        period_end="2024-12-31",
        period_type="y",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="as_reported",
        cpi_base_date="9999-01-01",
        mapping_version=1,
        as_of_before="2025-04-15T09:00:00+00:00",
        as_of_after="2025-05-08T12:00:00+00:00",
        field="value",
        expected_before=1.50,
        expected_after=1.75,
    ),
    AmendmentCase(
        case_id="synthetic-009-cash-equivalents-amend-tas29",
        entity_id="00000000-0000-0000-0000-000000000009",
        canonical_code="cash_and_equivalents",
        period_end="2024-12-31",
        period_type="y",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="cpi_normalized",
        cpi_base_date="2024-12-31",
        mapping_version=1,
        as_of_before="2025-03-20T08:00:00+00:00",
        as_of_after="2025-04-25T14:00:00+00:00",
        field="value",
        expected_before=2_100_000_000.0,
        expected_after=2_148_500_000.0,
    ),
    AmendmentCase(
        case_id="synthetic-010-inventory-amend-tas29",
        entity_id="00000000-0000-0000-0000-00000000000a",
        canonical_code="inventory",
        period_end="2025-03-31",
        period_type="q",
        consolidation="consolidated",
        currency_code="TRY",
        accounting_standard="ifrs",
        restatement_basis="cpi_normalized",
        cpi_base_date="2025-03-31",
        mapping_version=1,
        as_of_before="2025-04-22T10:30:00+00:00",
        as_of_after="2025-05-09T16:45:00+00:00",
        field="value",
        expected_before=875_400_000.0,
        expected_after=881_200_000.0,
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
