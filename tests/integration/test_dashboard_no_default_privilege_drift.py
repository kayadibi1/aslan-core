"""Migration 0020 must not install ``ALTER DEFAULT PRIVILEGES`` for
``aslan_dashboard``.

Spec §8.2: a future schema migration that creates a new table with PII
would otherwise auto-grant SELECT to the dashboard role and silently
bypass the column-allowlist contract. Default-privilege grants to
``aslan_dashboard`` are forbidden by construction; new tables MUST be
considered case-by-case in their own migration.

This is a source-parse test — no DB roundtrip — but lives in
``tests/integration/`` per the spec layout (its sibling tests do hit
the DB; co-locating reads naturally).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "aslan_core"
    / "db"
    / "migrations"
    / "versions"
    / "20260601_0001_0020_dashboard_role.py"
)

# Match any ALTER DEFAULT PRIVILEGES … GRANT … TO aslan_dashboard,
# tolerant of casing, line breaks, and intervening clauses
# (FOR ROLE / IN SCHEMA / GRANT … ON …). DOTALL lets ``.`` cross lines.
_DEFAULT_PRIV_PATTERN = re.compile(
    r"ALTER\s+DEFAULT\s+PRIVILEGES.*?GRANT.*?TO\s+aslan_dashboard",
    re.IGNORECASE | re.DOTALL,
)


def test_migration_0020_installs_no_default_privileges_for_dashboard() -> None:
    assert _MIGRATION_PATH.is_file(), f"migration file missing: {_MIGRATION_PATH}"
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    match = _DEFAULT_PRIV_PATTERN.search(source)
    if match is not None:
        start = max(0, match.start() - 40)
        end = match.end() + 40
        offending = source[start:end]
        raise AssertionError(
            "migration 0020 must NOT install ALTER DEFAULT PRIVILEGES for "
            "aslan_dashboard — every new table requires an explicit, reviewed "
            f"GRANT. Offending region: {offending!r}"
        )
