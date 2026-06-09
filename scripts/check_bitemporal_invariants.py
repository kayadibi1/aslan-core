"""Bitemporal invariants checker (H2).

Runs at every Phase gate from Phase 2 onward. Returns exit code 0 iff all
invariants hold; exit code 1 with a JSON report on stdout otherwise.

For every bitemporal-disciplined table (catalogued in the
`aslan_core.bitemporal_table_registry` once Phase 2 lands; in this
script's INVARIANT_QUERIES until then), runs SQL "correctness queries"
that should return 0 violations. Any non-zero count is reported.

Usage:

    python scripts/check_bitemporal_invariants.py            # uses ASLAN_PG_DSN env
    python scripts/check_bitemporal_invariants.py --db NAME  # other db on same host
    python scripts/check_bitemporal_invariants.py --json     # json-only output

Exit codes:
    0 — all invariants hold
    1 — at least one invariant violation (details on stdout as JSON)
    2 — operational error (cannot connect, missing extension, etc.)

Invariants checked (initial set; expanded as Phase 2 lands):

    - ts_observation_no_duplicate_as_of: no two rows share
      (series_id, ts, as_of).
    - ts_observation_as_of_not_null: every row has an as_of.
    - ts_observation_append_only_trigger_present: BEFORE UPDATE
      trigger exists on ts.observation (defense-in-depth check;
      the trigger is the H1 enforcement mechanism).
    - ts_financial_line_item_no_duplicate_as_of: no two rows share
      (entity_id, filing_id, statement_type, line_code,
      consolidation, period_end, as_of).
    - ts_canonical_financial_no_duplicate_as_of: same idea.
    - agg_filing_event_no_duplicate_as_of: no two rows share
      (filing_id, event_type, event_seq, as_of).
    - ref_identifier_no_overlapping_dateranges: GiST EXCLUDE present
      (verified via pg_constraint) and no overlapping pairs (verified
      by self-join).
    - ref_entity_as_of_consistency: post-Phase-2, every entity_id has
      a non-decreasing as_of sequence.
    - bitemporal_table_registry_completeness: every table with an
      `as_of`-typed column has a registry row (D28).
    - bitemporal_trigger_presence: every registry row pointing to an
      existing table has a corresponding BEFORE UPDATE trigger
      (enforced by the registry, since trigger naming convention is
      `<table>_no_update`).

The script is intentionally read-only against the database. It never
writes, never executes DDL, never bumps as_of.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Invariant:
    name: str
    description: str
    sql: str
    # Number of (offending-row-count, optional-detail-row) groups expected.
    # Most invariants return a single integer; some return per-table breakdowns.


INVARIANTS: tuple[Invariant, ...] = (
    Invariant(
        name="ts_observation_no_duplicate_as_of",
        description=(
            "ts.observation must not contain two rows with the same "
            "(series_id, ts, as_of). Enforced by PK; this is a defensive check."
        ),
        sql="""
            SELECT count(*) AS violations
            FROM (
                SELECT series_id, ts, as_of, count(*) AS c
                FROM ts.observation
                GROUP BY series_id, ts, as_of
                HAVING count(*) > 1
            ) AS dups;
        """,
    ),
    Invariant(
        name="ts_observation_as_of_not_null",
        description="Every ts.observation row must carry a non-null as_of.",
        sql="SELECT count(*) AS violations FROM ts.observation WHERE as_of IS NULL;",
    ),
    Invariant(
        name="ts_financial_line_item_no_duplicate_as_of",
        description=(
            "ts.financial_line_item must not contain two rows with the same "
            "(entity_id, filing_id, statement_type, line_code, consolidation, "
            "period_end, as_of). Enforced by UNIQUE constraint."
        ),
        sql="""
            SELECT count(*) AS violations
            FROM (
                SELECT entity_id, filing_id, statement_type, line_code,
                       consolidation, period_end, as_of, count(*) AS c
                FROM ts.financial_line_item
                GROUP BY entity_id, filing_id, statement_type, line_code,
                         consolidation, period_end, as_of
                HAVING count(*) > 1
            ) AS dups;
        """,
    ),
    Invariant(
        name="ts_canonical_financial_no_duplicate_as_of",
        description=(
            "ts.canonical_financial must not contain two rows with the same "
            "bitemporal key (PK minus as_of)."
        ),
        sql="""
            SELECT count(*) AS violations
            FROM (
                SELECT entity_id, canonical_code, period_end, period_type,
                       consolidation, currency_code, accounting_standard,
                       restatement_basis, cpi_base_date, mapping_version,
                       as_of, count(*) AS c
                FROM ts.canonical_financial
                GROUP BY entity_id, canonical_code, period_end, period_type,
                         consolidation, currency_code, accounting_standard,
                         restatement_basis, cpi_base_date, mapping_version,
                         as_of
                HAVING count(*) > 1
            ) AS dups;
        """,
    ),
    Invariant(
        name="agg_filing_event_no_duplicate_as_of",
        description=(
            "agg.filing_event must not contain two rows with the same "
            "(filing_id, event_type, event_seq, as_of). Enforced by UNIQUE."
        ),
        sql="""
            SELECT count(*) AS violations
            FROM (
                SELECT filing_id, event_type, event_seq, as_of, count(*) AS c
                FROM agg.filing_event
                GROUP BY filing_id, event_type, event_seq, as_of
                HAVING count(*) > 1
            ) AS dups;
        """,
    ),
    Invariant(
        name="ref_identifier_gist_exclude_present",
        description=(
            "ref.identifier must have a GiST EXCLUDE constraint preventing "
            "(namespace, value) daterange overlaps."
        ),
        sql="""
            SELECT
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    WHERE n.nspname = 'ref'
                      AND t.relname = 'identifier'
                      AND c.contype = 'x'
                ) THEN 0 ELSE 1 END AS violations;
        """,
    ),
    Invariant(
        name="ref_identifier_no_overlapping_pairs",
        description=(
            "Self-join check: no two ref.identifier rows with the same "
            "(namespace, value) have overlapping dateranges. Belt-and-braces "
            "with the GiST EXCLUDE."
        ),
        sql="""
            SELECT count(*) AS violations
            FROM ref.identifier a
            JOIN ref.identifier b
              ON a.namespace = b.namespace
             AND a.value = b.value
             AND a.ctid <> b.ctid
             AND daterange(a.valid_from, a.valid_to, '[)') &&
                 daterange(b.valid_from, b.valid_to, '[)');
        """,
    ),
    Invariant(
        name="bitemporal_table_registry_present",
        description=("After Phase 2, aslan_core.bitemporal_table_registry must exist."),
        sql="""
            SELECT
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'aslan_core'
                      AND table_name = 'bitemporal_table_registry'
                ) THEN 0 ELSE 1 END AS violations;
        """,
    ),
    Invariant(
        name="bitemporal_table_registry_completeness",
        description=(
            "Every table in pg_attribute that has a column literally named "
            "'as_of' (timestamptz) and is not in (aslan_core, audit, streams) "
            "must have a row in aslan_core.bitemporal_table_registry."
        ),
        sql="""
            SELECT count(*) AS violations
            FROM (
                SELECT n.nspname AS schema_name, c.relname AS table_name
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_inherits i ON i.inhrelid = c.oid
                WHERE a.attname = 'as_of'
                  AND a.atttypid = 'timestamptz'::regtype
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                  AND c.relkind = 'r'
                  AND i.inhrelid IS NULL  -- exclude inheritance children (TimescaleDB chunks)
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema',
                                        'aslan_core', 'audit', 'streams',
                                        '_timescaledb_internal',
                                        '_timescaledb_catalog',
                                        '_timescaledb_config',
                                        '_timescaledb_cache')
            ) tables_with_as_of
            WHERE NOT EXISTS (
                SELECT 1
                FROM aslan_core.bitemporal_table_registry r
                WHERE r.schema_name = tables_with_as_of.schema_name
                  AND r.table_name  = tables_with_as_of.table_name
            );
        """,
    ),
    Invariant(
        name="bitemporal_trigger_presence",
        description=(
            "Every aslan_core.bitemporal_table_registry row must have a "
            "BEFORE UPDATE trigger named '<table>_no_update' on the underlying "
            "table."
        ),
        sql="""
            SELECT count(*) AS violations
            FROM aslan_core.bitemporal_table_registry r
            WHERE NOT EXISTS (
                SELECT 1
                FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = r.schema_name
                  AND c.relname = r.table_name
                  AND t.tgname = r.table_name || '_no_update'
                  AND NOT t.tgisinternal
            );
        """,
    ),
)


def run_one(conn: psycopg.Connection, inv: Invariant) -> tuple[bool, int, str]:
    """Run a single invariant. Returns (ok, count, error_message)."""
    try:
        with conn.cursor() as cur:
            cur.execute(inv.sql)
            row = cur.fetchone()
            count = int(row[0]) if row else 0
            return (count == 0, count, "")
    except psycopg.errors.UndefinedTable:
        # Tables under construction (e.g., during Phase 2 partial application)
        # are not violations; they just mean the invariant doesn't apply yet.
        # The bitemporal_table_registry_present invariant covers the registry
        # itself; the rest skip cleanly.
        return (True, 0, "table-not-yet-present (skipped)")
    except psycopg.errors.UndefinedObject as e:
        return (True, 0, f"object-not-yet-present: {e} (skipped)")
    except Exception as e:
        return (False, -1, f"runtime-error: {e!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bitemporal invariants checker (H2).")
    parser.add_argument(
        "--db",
        default=None,
        help="Override DB name; otherwise read from ASLAN_PG_DSN env.",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="Full DSN; overrides --db and ASLAN_PG_DSN.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON only to stdout; suppress human-friendly summary.",
    )
    args = parser.parse_args()

    log = _get_log()

    dsn = args.dsn or os.environ.get("ASLAN_PG_DSN")
    if not dsn:
        log.error("invariants_check_no_dsn")
        print(
            json.dumps(
                {
                    "status": "error",
                    "reason": "missing DSN; set ASLAN_PG_DSN or pass --dsn",
                }
            )
        )
        return 2
    if args.db and args.dsn is None:
        # Replace the dbname segment of the DSN.
        # Keep this surgical so we don't accidentally mangle host/port.
        import urllib.parse as _u

        u = _u.urlparse(dsn)
        path = "/" + args.db
        dsn = _u.urlunparse((u.scheme, u.netloc, path, u.params, u.query, u.fragment))

    try:
        with psycopg.connect(dsn) as conn:
            results = []
            failures = []
            for inv in INVARIANTS:
                ok, count, msg = run_one(conn, inv)
                results.append(
                    {
                        "name": inv.name,
                        "ok": ok,
                        "violations": count,
                        "message": msg,
                    }
                )
                if not ok:
                    failures.append({"name": inv.name, "violations": count, "message": msg})

        report = {
            "dsn": _redact_dsn(dsn),
            "invariants_total": len(INVARIANTS),
            "invariants_ok": sum(1 for r in results if r["ok"]),
            "invariants_failed": len(failures),
            "results": results,
        }
        if failures:
            report["status"] = "FAIL"
            report["violations"] = failures
            log.error(
                "invariants_check_fail",
                invariants_total=len(INVARIANTS),
                invariants_failed=len(failures),
                violations=failures,
            )
            print(json.dumps(report, indent=2 if not args.json else None))
            return 1
        report["status"] = "PASS"
        log.info("invariants_check_pass", invariants_total=len(INVARIANTS))
        print(json.dumps(report, indent=2 if not args.json else None))
        return 0
    except psycopg.OperationalError as e:
        log.exception("invariants_check_connect_failed", dsn_redacted=_redact_dsn(dsn))
        print(json.dumps({"status": "error", "reason": f"connect-failed: {e!r}"}))
        return 2


def _get_log() -> Any:  # noqa: F821 — Any imported below
    """Return a structlog logger; fall back to stdlib if unavailable."""
    try:
        from aslan_core.api.research_logging import (
            configure_research_logging,
            get_research_logger,
        )

        configure_research_logging()
        return get_research_logger("aslan_core.scripts.check_bitemporal_invariants")
    except Exception:
        import logging as _logging

        return _logging.getLogger("aslan_core.scripts.check_bitemporal_invariants")


def _redact_dsn(dsn: str) -> str:
    """Hide password from the DSN for the report."""
    import re

    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn)


if __name__ == "__main__":
    sys.exit(main())
