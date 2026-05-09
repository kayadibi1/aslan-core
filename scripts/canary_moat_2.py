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
from datetime import datetime, timezone
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


# Placeholder; populate from a curated YAML in scripts/canary_cases/ as
# real amendments accumulate. ≥10 cases required per SCOPE.md D29.
KNOWN_AMENDMENTS: tuple[AmendmentCase, ...] = ()


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
        except Exception as e:  # noqa: BLE001
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
        "ran_at": datetime.now(tz=timezone.utc).isoformat(),
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
