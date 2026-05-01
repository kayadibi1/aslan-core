"""``queries.py`` itself is scanner-clean under queries.py rules.

Spec §5.1 + §8.1: in queries.py mode the scanner permits
``sqlalchemy.text`` (the only legitimate SQL construction primitive),
but enforces:

  * Every ``text(_)`` call's first argument is a ``Constant(str)``.
    F-string (``JoinedStr``), concatenation (``BinOp``), and bare
    name reference are forbidden.
  * The literal does NOT contain forbidden substrings:
    ``FOR UPDATE`` / ``FOR SHARE``, ``LOCK TABLE``, ``pg_advisory_``,
    ``SET default_transaction_read_only``, ``BEGIN READ WRITE``,
    ``RESET ROLE``, ``SET ROLE``.
  * The literal does NOT contain a non-trailing ``;`` statement
    separator (defense against multi-statement injection).
  * No reference to runtime-introspection / dynamic-import / code-
    bypass / sandbox-escape primitives (these ``ALWAYS_FORBIDDEN_STUBS``
    are rejected even in queries.py mode).
"""

from __future__ import annotations

from pathlib import Path

from aslan_core.dashboard._static_sql_scan import scan

_QUERIES_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "aslan_core" / "dashboard" / "queries.py"
)


def test_queries_py_is_scanner_clean() -> None:
    source = _QUERIES_PATH.read_text(encoding="utf-8")
    findings = scan(source, is_queries=True)
    assert findings == [], f"queries.py produced findings: {findings}"
