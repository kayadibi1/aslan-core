"""Alias-aware AST scanner for the v0.6.0 dashboard's static-SQL discipline.

Spec §5.1 + codex rounds 4-6: ``queries.py`` is the ONLY module in
``aslan_core.dashboard`` that may construct SQL via SQLAlchemy
``text(...)``. Every other module imports query helpers by name. The
scanner here is invoked by three test files (Task 5):

  * ``test_dashboard_no_sqlalchemy_expression_api.py`` — runs the
    scanner against 21+ ``EVIL_STUBS`` (one per known bypass shape)
    and asserts each is rejected.
  * ``test_dashboard_static_sql_only.py`` — scans every dashboard
    module other than ``queries.py``; result must be empty.
  * ``test_dashboard_queries_have_static_sql_only.py`` — scans
    ``queries.py`` itself with the inside-``text()`` restrictions
    (no f-string, no concatenation, no forbidden substrings).

This is best-effort lint, not a sound static analyzer. It assumes the
dashboard package does not deliberately shadow forbidden names with
local variables. The dashboard is intentionally small and auditable
so that assumption is cheap to maintain.

Detection strategy (single AST walk):

  1. Build a symbol table from every module-level ``Import``,
     ``ImportFrom``, and bare ``Assign`` (alias chains like
     ``sa = sqlalchemy``). Function-scoped assigns are intentionally
     not tracked — keeps false-positive rate low.
  2. Walk every node and check Name / Attribute / Subscript / Call
     against:
       - forbidden bare names (``eval``, ``exec``, ``compile``,
         ``__import__``, ``getattr``, ``setattr``, ``hasattr``,
         ``vars``, ``globals``, ``locals``);
       - forbidden qualified names (``importlib.import_module``,
         ``operator.attrgetter``, ``sys.modules``, …);
       - forbidden SQLAlchemy expression-API symbols
         (``sqlalchemy.text``, ``sqlalchemy.select``, ``sqlalchemy.func``,
         …) with prefix-match so ``sqlalchemy.func.pg_advisory_lock``
         is also caught;
       - forbidden attribute accesses regardless of receiver
         (``__class__``, ``__subclasses__``, ``__mro__``,
         ``exec_driver_sql``, ``with_for_update``, …);
       - ``getattr(_, "string")`` where ``"string"`` is a forbidden
         attribute name (closes the dynamic-attribute bypass).

  3. In ``queries.py`` mode, ``sqlalchemy.text`` is permitted, but
     each ``text(_)`` call's first argument must be a ``Constant(str)``,
     and the literal must not contain forbidden substrings
     (``FOR UPDATE``, ``LOCK TABLE``, ``pg_advisory_``, …).
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Finding:
    """Single rejection from the scanner."""

    line: int
    kind: str
    message: str


# ── Forbidden name sets ──────────────────────────────────────────


# Bare names that are forbidden anywhere in the dashboard package
# (queries.py included). These are runtime-introspection / code-bypass
# primitives — none have a legitimate use in static-SQL code.
_FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "__import__",
        "eval",
        "exec",
        "compile",
        "getattr",
        "setattr",
        "hasattr",
        "vars",
        "globals",
        "locals",
    }
)

# Fully-qualified names forbidden anywhere (Attribute chains).
_FORBIDDEN_QUALIFIED: frozenset[str] = frozenset(
    {
        "importlib.import_module",
        "importlib.__import__",
        "operator.attrgetter",
        "operator.methodcaller",
        "sys.modules",
    }
)

# SQLAlchemy expression-API symbols. Each is a prefix-matchable
# qualified name; ``sqlalchemy.func.pg_advisory_lock`` is rejected
# because its prefix ``sqlalchemy.func`` is in this set.
_SQLALCHEMY_EXPRESSION: frozenset[str] = frozenset(
    {
        "sqlalchemy.text",
        "sqlalchemy.select",
        "sqlalchemy.func",
        "sqlalchemy.literal_column",
        "sqlalchemy.Table",
        "sqlalchemy.column",
        "sqlalchemy.bindparam",
        "sqlalchemy.literal",
        "sqlalchemy.cast",
    }
)

# Attribute names forbidden regardless of receiver — driver-API,
# row-locking, and dunder access.
_FORBIDDEN_ATTRS: frozenset[str] = frozenset(
    {
        # Driver-API
        "exec_driver_sql",
        "execute_options",
        "driver_connection",
        # Row-locking
        "with_for_update",
        "with_for_share",
        "with_lockmode",
        # Dunder attribute walks
        "__getattribute__",
        "__getattr__",
        "__dict__",
        "__class__",
        "__subclasses__",
        "__bases__",
        "__mro__",
    }
)

# Substrings forbidden inside any ``text("…")`` literal in queries.py.
# Case-insensitive substring search.
_FORBIDDEN_SQL_SUBSTRINGS: tuple[str, ...] = (
    "FOR UPDATE",
    "FOR SHARE",
    "LOCK TABLE",
    "pg_advisory_",
    "SET default_transaction_read_only",
    "BEGIN READ WRITE",
    "RESET ROLE",
    "SET ROLE",
)

# ── Codex F-3: queries.py column-allowlist ───────────────────────


# Mirrors the GRANT lists in migrations 0020 / 0021 / 0022. Each
# entry is the set of columns the ``aslan_dashboard`` role can SELECT.
# A column reference in queries.py that is not in the role's allowlist
# would fail at runtime with ``InsufficientPrivilegeError`` (the
# load-bearing GDPR boundary); this lint catches the same gap at
# CI time.
#
# Updating these constants WITHOUT updating the migrations is itself
# a bug — the migrations are authoritative. The lint and the
# migrations co-evolve.
_COLUMN_ALLOWLIST: dict[str, frozenset[str]] = {
    "streams.outbox": frozenset(
        {
            "outbox_id",
            "stream_name",
            "event_id",
            "schema_version",
            "producer_run_id",
            "source_id",
            "created_at",
            "published_at",
            "redis_message_id",
            "publish_attempts",
            "last_attempt_at",
            "actor_id",
            "actor_kind",
            "request_id",
            # client_ip + user_agent revoked in migration 0022 (codex F-1).
        }
    ),
    "streams.deadletter_log": frozenset(
        {
            "failure_id",
            "stream_name",
            "deadletter_stream",
            "event_id",
            "original_message_id",
            "group_name",
            "consumer_name",
            "failure_count",
            "routed_at",
            "routed_at_redis",
            "redis_message_id",
            "actor_id",
            "actor_kind",
            "request_id",
        }
    ),
    "streams.redaction_registry": frozenset(
        {
            "event_id",
            "redaction_reason",
            "redacted_at",
            "original_stream",
            "redacted_payload_hash",
            "original_payload_hash",
            "actor_id",
            "actor_kind",
            "request_id",
        }
    ),
    "src.ingestion_run": frozenset(
        {
            "ingestion_run_id",
            "source_id",
            "job_name",
            "started_at",
            "finished_at",
            "status",
            "error_count",
            "rows_written",
            "docs_written",
            "bytes_written",
            "config_hash",
            "metadata",
            "actor_id",
            "actor_kind",
        }
    ),
    "doc.filing": frozenset(
        {
            "filing_id",
            "source_id",
            "source_filing_ref",
            "entity_id",
            "kind",
            "subkind",
            "language",
            "published_at",
            "period_start",
            "period_end",
            "source_url",
            "is_amendment",
            "previous_filing_id",
            "primary_object_key",
            "primary_mime",
            "primary_sha256",
            "primary_bytes",
            "extracted_text_key",
            "has_xbrl",
            "xbrl_object_key",
            "metadata",
            "ingestion_run_id",
            "discovered_at",
            "revision_no",
            "actor_id",
            "actor_kind",
            "request_id",
            # client_ip + user_agent revoked in 0022 (codex F-1).
        }
    ),
    "doc.filing_body": frozenset(
        {
            "filing_id",
            "body_lang",
            "extracted_at",
            "actor_id",
            "actor_kind",
            "request_id",
            # client_ip + user_agent revoked in 0022 (codex F-1).
        }
    ),
    "audit.events": frozenset(
        {
            "event_id",
            "occurred_at",
            "actor_id",
            "actor_kind",
            "request_id",
            "ingestion_run_id",
            "operation",
            "target_schema",
            "target_table",
            "target_pk",
            # client_ip + user_agent revoked in 0022; the truncated
            # CIDR is reachable only via the SECURITY DEFINER helper
            # ``audit.event_client_ip_truncated``. ``before`` /
            # ``after`` / ``metadata`` were never granted.
        }
    ),
}

# Tables the dashboard role has full table-level SELECT on. No
# column-allowlist; any column reference is permitted.
_FULL_TABLE_GRANTS: frozenset[str] = frozenset(
    {
        "streams.event_id_to_redis",
        "streams.deadletter_redis_index",
        "streams.deadletter_xadd_intent",
        "src.source",
        "ts.series_catalog",
        "ts.observation",
        "audit.observation_batch_keys",
        "ref.entity",
        "ref.identifier",
        "ref.currency",
        "ref.sector",
        "doc.filing_attachment",
    }
)


def scan(source: str, *, is_queries: bool = False) -> list[Finding]:
    """Scan a Python source string. Returns a list of rejections.

    :param source: Python source code as text. Parsed via ``ast.parse``.
    :param is_queries: True when scanning ``queries.py``. Permits
        ``sqlalchemy.text`` as the only SQLAlchemy import; enforces
        the inside-``text()`` literal-only + forbidden-substring
        restrictions.
    """
    tree = ast.parse(source)
    scanner = _Scanner(is_queries=is_queries)
    scanner.collect_aliases(tree)
    scanner.visit(tree)
    return scanner.findings


class _Scanner(ast.NodeVisitor):
    def __init__(self, *, is_queries: bool) -> None:
        self.is_queries = is_queries
        self.symbols: dict[str, str] = {}
        self.findings: list[Finding] = []

    # ── Phase 1: build symbol table ──────────────────────────────

    def collect_aliases(self, tree: ast.AST) -> None:
        """Build the alias table.

        Imports are collected from EVERY scope (codex F-2): a function-
        local ``from sqlalchemy import text`` is just as much a bypass
        as a module-level one, and the symbol it binds shadows enclosing
        scopes. We treat all imports as if they were module-level for
        the purposes of name resolution. Module-level ``Assign`` aliases
        (``sa = sqlalchemy``) are still only collected from the top-level
        body to avoid false positives from local rebinds — those have
        no legitimate use in dashboard code anyway.
        """
        if not isinstance(tree, ast.Module):
            return
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self._handle_import(node)
            elif isinstance(node, ast.ImportFrom):
                self._handle_import_from(node)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                self._handle_assign(node)

    def _handle_import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                # ``import sqlalchemy.func as fn`` → fn → sqlalchemy.func
                self.symbols[alias.asname] = alias.name
            else:
                # ``import sqlalchemy`` → sqlalchemy → sqlalchemy
                # ``import sqlalchemy.func`` → sqlalchemy → sqlalchemy
                #   (the dotted path is bound under the head name)
                head = alias.name.split(".")[0]
                self.symbols[head] = head

    def _handle_import_from(self, node: ast.ImportFrom) -> None:
        if node.module is None:
            return
        for alias in node.names:
            local = alias.asname or alias.name
            self.symbols[local] = f"{node.module}.{alias.name}"

    def _handle_assign(self, node: ast.Assign) -> None:
        # Only handle simple ``x = name_or_attribute`` at module level.
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            return
        resolved = self._resolve(node.value)
        if resolved is not None:
            self.symbols[node.targets[0].id] = resolved

    # ── Phase 2: resolve qualified names ──────────────────────────

    def _resolve(self, node: ast.AST) -> str | None:
        """Return the qualified name a Name/Attribute chain resolves to,
        per the symbol table. Returns None for unresolvable nodes
        (e.g. function calls, subscripts, literals)."""
        if isinstance(node, ast.Name):
            return self.symbols.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self._resolve(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    # ── Phase 3: detection ───────────────────────────────────────

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in _FORBIDDEN_NAMES:
            self._reject(node, f"forbidden name: {node.id}")
        else:
            qualified = self.symbols.get(node.id)
            if qualified is not None:
                self._check_qualified(node, qualified)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _FORBIDDEN_ATTRS:
            self._reject(node, f"forbidden attribute access: .{node.attr}")
        qualified = self._resolve(node)
        if qualified is not None:
            self._check_qualified(node, qualified)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        base_qualified = self._resolve(node.value)
        if base_qualified == "sys.modules":
            self._reject(node, "subscript access to sys.modules forbidden")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # ``getattr(_, "string")`` where the string is a forbidden
        # attribute name (or any string — we reject all string-literal
        # getattrs as defense in depth).
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            self._reject(
                node,
                f"getattr(_, {node.args[1].value!r}) forbidden — "
                f"resolve attribute access statically",
            )

        # ``sqlalchemy.text(_)`` in queries.py: enforce literal-only +
        # forbidden-substring restrictions.
        if self.is_queries:
            resolved = self._resolve(node.func)
            if resolved == "sqlalchemy.text":
                self._check_text_call(node)

        self.generic_visit(node)

    # ── Phase 4: checks ──────────────────────────────────────────

    def _check_qualified(self, node: ast.AST, qualified: str) -> None:
        if qualified in _FORBIDDEN_QUALIFIED or any(
            qualified == q or qualified.startswith(q + ".") for q in _FORBIDDEN_QUALIFIED
        ):
            self._reject(node, f"forbidden qualified name: {qualified}")
            return
        for forbidden in _SQLALCHEMY_EXPRESSION:
            if qualified == forbidden or qualified.startswith(forbidden + "."):
                # In queries.py, ``sqlalchemy.text`` is the only
                # permitted SQLAlchemy expression-API symbol.
                if self.is_queries and qualified == "sqlalchemy.text":
                    return
                self._reject(node, f"forbidden SQLAlchemy expression API: {qualified}")
                return

    def _check_text_call(self, node: ast.Call) -> None:
        if not node.args:
            self._reject(node, "sqlalchemy.text() called with no arguments")
            return
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            self._reject(
                node,
                "sqlalchemy.text() first argument must be a string literal — "
                "f-string, concatenation, and name reference are forbidden",
            )
            return
        sql = first.value
        upper = sql.upper()
        for sub in _FORBIDDEN_SQL_SUBSTRINGS:
            if sub.upper() in upper:
                self._reject(
                    node,
                    f"sqlalchemy.text() literal contains forbidden substring: {sub!r}",
                )
        # Reject statement separators other than the trailing one.
        # SQLAlchemy text() can drive multi-statement bodies through
        # raw connections; a leading or interior ``;`` lets a future
        # injection daisy-chain a privilege escalation.
        stripped = sql.rstrip().rstrip(";").rstrip()
        if ";" in stripped:
            self._reject(
                node,
                "sqlalchemy.text() literal contains a non-trailing ';' statement separator",
            )
        # Codex F-3: parse SQL via sqlglot and verify every column
        # reference is in the dashboard role's GRANT allowlist.
        self._check_column_allowlist(sql, node)

    def _check_column_allowlist(self, sql: str, ast_node: ast.AST) -> None:
        """Codex F-3: parse ``sql`` via sqlglot and reject any column
        reference whose ``(table, column)`` pair is not in
        ``_COLUMN_ALLOWLIST`` and whose table is not in
        ``_FULL_TABLE_GRANTS``.

        Lazy-imported so the dashboard runtime is not forced to
        install ``sqlglot`` (it's a dev-group dep, used only by the
        lint). If sqlglot is unavailable the check is skipped — the
        privilege layer remains the load-bearing boundary; the lint
        is defense in depth on top of it.
        """
        try:
            import sqlglot
            from sqlglot.errors import ParseError
            from sqlglot.optimizer.scope import traverse_scope
        except ImportError:
            return

        try:
            parsed = sqlglot.parse_one(sql, dialect="postgres")
        except ParseError as exc:
            self._reject(
                ast_node,
                f"queries.py: sqlalchemy.text() literal failed to parse "
                f"under PostgreSQL dialect — {exc}",
            )
            return

        for scope in traverse_scope(parsed):
            for col in scope.columns:
                full_table = self._resolve_column_table(col, scope)
                if full_table is None:
                    continue
                if full_table in _FULL_TABLE_GRANTS:
                    continue
                allowlist = _COLUMN_ALLOWLIST.get(full_table)
                if allowlist is None:
                    # Not a checked table (e.g. pg_catalog, an extension).
                    continue
                if col.name not in allowlist:
                    self._reject(
                        ast_node,
                        f"queries.py: column reference {full_table}.{col.name} "
                        f"is NOT in the aslan_dashboard role's GRANT allowlist "
                        f"(migrations 0020/0021/0022). The privilege layer "
                        f"would reject this at runtime; the lint catches it "
                        f"at CI time (codex F-3).",
                    )

    @staticmethod
    def _resolve_column_table(col: Any, scope: Any) -> str | None:
        """Resolve ``col.table`` to a fully-qualified ``schema.table``
        via ``scope`` and its parent chain. Returns ``None`` for
        unresolvable references (an unknown alias likely indicates a
        SQL bug; we don't reject because the privilege layer will)."""
        # Unqualified column: only resolvable if the immediate scope
        # has exactly one table.
        if not col.table:
            tables = list(scope.tables)
            if len(tables) != 1:
                return None
            t = tables[0]
            schema = t.db or ""
            return f"{schema}.{t.name}" if schema else t.name

        # Qualified column: walk current → parent scopes.
        current = scope
        while current is not None:
            for t in current.tables:
                alias = t.alias or t.name
                if alias == col.table:
                    schema = t.db or ""
                    return f"{schema}.{t.name}" if schema else t.name
            current = current.parent
        return None

    # ── Reporter ─────────────────────────────────────────────────

    def _reject(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 0)
        self.findings.append(Finding(line=line, kind="forbidden", message=message))


__all__ = ["Finding", "scan"]


def _all_dashboard_modules() -> Iterable[str]:
    """Helper for the test suite: yield the absolute paths of every
    ``aslan_core.dashboard`` Python module other than this scanner
    itself."""
    from pathlib import Path

    here = Path(__file__).resolve()
    package_root = here.parent
    for path in package_root.rglob("*.py"):
        if path == here:
            continue
        yield str(path)
