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


# ── Codex F-3 column-allowlist tests ──────────────────────────────


def _wrap_in_text(sql: str) -> str:
    """Build a synthetic queries.py-shaped source: a single
    ``op = text("…")`` statement at module level."""
    sql_repr = sql.replace("\n", " ").strip()
    return f'from sqlalchemy import text\nq = text("{sql_repr}")\n'


def test_forbidden_column_select_is_caught() -> None:
    """Spec §6.3 + codex F-3: ``client_ip`` is not in the dashboard
    role's GRANT for ``audit.events`` (migration 0022 revoked it).
    The lint catches a queries.py reference to it."""
    source = _wrap_in_text("SELECT client_ip FROM audit.events")
    findings = scan(source, is_queries=True)
    assert findings, "scanner missed forbidden client_ip reference"
    msgs = " ".join(f.message for f in findings)
    assert "audit.events.client_ip" in msgs


def test_forbidden_payload_column_is_caught() -> None:
    """``payload`` is forbidden on ``streams.outbox`` (codex F1-F4
    GDPR boundary). The lint catches a direct reference."""
    source = _wrap_in_text("SELECT payload FROM streams.outbox")
    findings = scan(source, is_queries=True)
    assert findings
    assert any("streams.outbox.payload" in f.message for f in findings)


def test_forbidden_metadata_is_caught_on_every_table() -> None:
    """``metadata`` is forbidden on ``audit.events`` (rendered via
    the ``event_metadata_key_count`` SECURITY DEFINER helper).

    Codex post-impl HIGH: migration 0023 also revoked ``metadata``
    on ``src.ingestion_run`` and ``doc.filing`` so the privilege
    layer matches the in-code VM contract that treats every JSONB
    metadata blob as forbidden. The lint mirrors the GRANT and
    rejects all three."""
    bad_audit = _wrap_in_text("SELECT metadata FROM audit.events")
    findings = scan(bad_audit, is_queries=True)
    assert findings
    assert any("audit.events.metadata" in f.message for f in findings)

    bad_src = _wrap_in_text("SELECT metadata FROM src.ingestion_run")
    findings = scan(bad_src, is_queries=True)
    assert findings
    assert any("src.ingestion_run.metadata" in f.message for f in findings)

    bad_doc = _wrap_in_text("SELECT metadata FROM doc.filing")
    findings = scan(bad_doc, is_queries=True)
    assert findings
    assert any("doc.filing.metadata" in f.message for f in findings)


def test_forbidden_column_via_alias_is_caught() -> None:
    """Aliased table refs resolve correctly. ``a.client_ip`` in
    ``FROM audit.events a`` is rejected just like the unqualified form."""
    source = _wrap_in_text("SELECT a.client_ip FROM audit.events a")
    findings = scan(source, is_queries=True)
    assert findings
    assert any("audit.events.client_ip" in f.message for f in findings)


def test_forbidden_column_in_correlated_subquery_is_caught() -> None:
    """A correlated subquery references the parent scope's alias.
    The scope-chain walk must resolve it correctly so the lint
    flags the forbidden column even at one level of nesting."""
    sql = (
        "SELECT f.filing_id, "
        "(SELECT count(*) FROM doc.filing_attachment a WHERE a.filing_id = f.filing_id) AS n "
        "FROM doc.filing f WHERE f.title = 'leak'"
    )
    findings = scan(_wrap_in_text(sql), is_queries=True)
    assert findings
    assert any("doc.filing.title" in f.message for f in findings)


def test_full_table_grant_columns_are_unrestricted() -> None:
    """Tables granted full table-level SELECT (``ts.observation``,
    ``src.source``, ``ref.entity``, ``doc.filing_attachment`` …)
    have no column-allowlist; any column reference is allowed."""
    source = _wrap_in_text("SELECT some_arbitrary_column FROM ts.observation")
    assert scan(source, is_queries=True) == []


def test_security_definer_helper_call_is_not_misinterpreted_as_column() -> None:
    """``audit.event_metadata_key_count(event_id, occurred_at)`` is
    a function call, not a ``audit.event_metadata_key_count`` column
    reference. The lint must NOT flag the function name."""
    sql = "SELECT audit.event_metadata_key_count(event_id, occurred_at) AS n FROM audit.events"
    findings = scan(_wrap_in_text(sql), is_queries=True)
    assert findings == [], f"function call misclassified as column reference; findings: {findings}"


def test_unparseable_sql_is_rejected() -> None:
    """A SQL literal sqlglot can't parse is itself suspicious — reject
    so a malformed migration doesn't silently slip past the lint."""
    source = _wrap_in_text("SELECT FROM WHERE ORDER BY GROUP BY")
    findings = scan(source, is_queries=True)
    assert findings


def test_column_allowlist_check_does_not_run_in_non_queries_mode() -> None:
    """The column-allowlist runs only in queries.py mode — non-
    queries modules are already forbidden from importing
    ``sqlalchemy.text``, so SQL parsing never gets there. Sanity:
    a forbidden column reference in a non-queries stub triggers the
    EXISTING ``sqlalchemy.text`` rejection, not a column-allowlist
    one."""
    source = "from sqlalchemy import text\nq = text('SELECT client_ip FROM audit.events')\n"
    findings = scan(source, is_queries=False)
    assert findings
    msgs = " ".join(f.message for f in findings)
    # Must reject for the import-not-allowed reason; the column-
    # allowlist message is gated by queries.py mode.
    assert "sqlalchemy" in msgs.lower() or "forbidden" in msgs.lower()
