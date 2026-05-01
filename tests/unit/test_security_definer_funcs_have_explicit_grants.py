"""Lint: every alembic migration that CREATEs a SECURITY DEFINER
function MUST also REVOKE EXECUTE FROM PUBLIC and GRANT EXECUTE TO
<role> for that exact signature in the same file.

Spec §8.1 + codex round-5: PG defaults to ``GRANT EXECUTE ON FUNCTION
… TO PUBLIC`` at creation. A SECURITY DEFINER without an explicit
REVOKE is callable by every role with ``USAGE`` on the schema —
typically the entire data-plane. The lint catches a future migration
that ships a SECURITY DEFINER without locking down its ACL.

The check walks the AST of each migration file in ``versions/``,
extracts every string passed to ``op.execute(...)`` (handling both
bare strings and ``text(...).bindparams(...)`` chains), and asserts:

  * Every ``CREATE [OR REPLACE] FUNCTION schema.name(args) … SECURITY
    DEFINER …`` has a same-file ``REVOKE EXECUTE ON FUNCTION
    schema.name(args) FROM PUBLIC`` (PUBLIC may co-occur with other
    targets in the REVOKE list).
  * Same-file ``GRANT EXECUTE ON FUNCTION schema.name(args) TO <role>``
    for at least one role.

Argument-list normalization strips PG parameter names (``p_*``) and
length specifiers so ``CHAR(64)`` and ``char`` match (PG canonicalizes
to the same type internally).

Grandfathered exceptions (do NOT extend without a follow-up migration
that closes the gap):

  * ``0018`` / ``streams.redaction_registry_insert`` was created with
    a same-file ``GRANT EXECUTE … TO aslan_app`` but no ``REVOKE
    EXECUTE … FROM PUBLIC``. Migration 0020 issues the REVOKE
    retroactively. The final head schema has the contract; the lint
    relaxes the per-file requirement here for historical reasons.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_VERSIONS_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "aslan_core" / "db" / "migrations" / "versions"
)


# Each entry: (revision, "schema.function_name", frozenset of relaxed
# requirements among {"revoke", "grant"}).
_GRANDFATHERED: dict[tuple[str, str], frozenset[str]] = {
    ("0018", "streams.redaction_registry_insert"): frozenset({"revoke"}),
}


def _extract_op_execute_strings(source: str) -> list[str]:
    """Return every string literal passed as the first arg to
    ``op.execute(...)`` in the parsed module. Handles direct
    ``op.execute("…")`` and ``op.execute(text("…").bindparams(...))``
    chains. Ignores f-strings (``op.execute(f"…")``) — the lint targets
    fully-static SECURITY DEFINER definitions; an f-string CREATE
    would be a separate review concern."""
    tree = ast.parse(source)
    results: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "execute"
            and isinstance(func.value, ast.Name)
            and func.value.id == "op"
        ):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            results.append(first.value)
            continue
        # text("…").bindparams(...) and similar chains: walk down .attr calls
        # until we find a top-level call whose func is the text() name.
        inner: ast.AST = first
        while isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
            inner = inner.func.value
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, (ast.Name, ast.Attribute))
            and (
                (isinstance(inner.func, ast.Name) and inner.func.id == "text")
                or (isinstance(inner.func, ast.Attribute) and inner.func.attr == "text")
            )
            and inner.args
            and isinstance(inner.args[0], ast.Constant)
            and isinstance(inner.args[0].value, str)
        ):
            results.append(inner.args[0].value)
    return results


def _normalize_args(args: str) -> str:
    """Lower-case PG argument list with parameter names stripped.

    Examples:
        ``p_event_id BIGINT, p_occurred_at TIMESTAMPTZ`` → ``bigint, timestamptz``
        ``UUID, TEXT, TIMESTAMPTZ, TEXT, JSONB, CHAR(64), CHAR(64)``
            → ``uuid, text, timestamptz, text, jsonb, char, char``
    """
    parts: list[str] = []
    for raw in args.split(","):
        tok = raw.strip()
        if not tok:
            continue
        atoms = tok.split()
        # Strip a leading PG parameter name (``p_*``).
        if len(atoms) >= 2 and atoms[0].lower().startswith("p_"):
            tok = " ".join(atoms[1:])
        # Drop any length / precision parens (``char(64)`` → ``char``).
        tok = re.sub(r"\s*\([^)]*\)", "", tok)
        parts.append(tok.lower().strip())
    return ", ".join(parts)


# Argument list capture: allows depth-1 nested parens inside type
# signatures (``CHAR(64)``, ``NUMERIC(10,2)``).
_ARG_LIST = r"((?:[^()]|\([^)]*\))*)"

_CREATE_FUNCTION_RE = re.compile(
    rf"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(\w+)\.(\w+)\s*\({_ARG_LIST}\)",
    re.IGNORECASE | re.DOTALL,
)
_SECURITY_DEFINER_RE = re.compile(r"\bSECURITY\s+DEFINER\b", re.IGNORECASE)
_REVOKE_EXEC_RE = re.compile(
    rf"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+(\w+)\.(\w+)\s*\({_ARG_LIST}\)\s+FROM\s+([\w\s,]+?)(?:;|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_GRANT_EXEC_RE = re.compile(
    rf"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+(\w+)\.(\w+)\s*\({_ARG_LIST}\)\s+TO\s+([\w\s,]+?)(?:;|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_REVISION_RE = re.compile(r'^\s*revision\s*:?\s*str\s*=\s*"([^"]+)"', re.MULTILINE)


def _revision_id(source: str, fallback: str) -> str:
    m = _REVISION_RE.search(source)
    return m.group(1) if m else fallback


def test_security_definer_funcs_have_explicit_grants() -> None:
    failures: list[str] = []
    for migration in sorted(_VERSIONS_DIR.glob("*.py")):
        if migration.name == "__init__.py":
            continue
        source = migration.read_text(encoding="utf-8")
        revision = _revision_id(source, fallback=migration.stem)
        sql_strings = _extract_op_execute_strings(source)

        secdef_signatures: list[tuple[str, str, str]] = []
        for sql in sql_strings:
            if not _SECURITY_DEFINER_RE.search(sql):
                continue
            for m in _CREATE_FUNCTION_RE.finditer(sql):
                schema = m.group(1).lower()
                name = m.group(2).lower()
                args = _normalize_args(m.group(3))
                secdef_signatures.append((schema, name, args))

        if not secdef_signatures:
            continue

        revokes: set[tuple[str, str, str]] = set()
        grants: set[tuple[str, str, str]] = set()
        for sql in sql_strings:
            for m in _REVOKE_EXEC_RE.finditer(sql):
                targets = {t.strip().lower() for t in m.group(4).split(",")}
                if "public" in targets:
                    revokes.add(
                        (
                            m.group(1).lower(),
                            m.group(2).lower(),
                            _normalize_args(m.group(3)),
                        )
                    )
            for m in _GRANT_EXEC_RE.finditer(sql):
                grants.add(
                    (
                        m.group(1).lower(),
                        m.group(2).lower(),
                        _normalize_args(m.group(3)),
                    )
                )

        for sig in secdef_signatures:
            qualname = f"{sig[0]}.{sig[1]}"
            grandfathered = _GRANDFATHERED.get((revision, qualname), frozenset())
            if sig not in revokes and "revoke" not in grandfathered:
                failures.append(
                    f"{migration.name} (rev {revision}): "
                    f"{qualname}({sig[2]}) is CREATEd SECURITY DEFINER but "
                    f"the same migration has no REVOKE EXECUTE ON FUNCTION "
                    f"{qualname}({sig[2]}) FROM PUBLIC."
                )
            if sig not in grants and "grant" not in grandfathered:
                failures.append(
                    f"{migration.name} (rev {revision}): "
                    f"{qualname}({sig[2]}) is CREATEd SECURITY DEFINER but "
                    f"the same migration has no GRANT EXECUTE ON FUNCTION "
                    f"{qualname}({sig[2]}) TO <role>."
                )

    assert not failures, "SECURITY DEFINER lint:\n  - " + "\n  - ".join(failures)
