"""Scanner rejects every known SQLAlchemy / runtime-introspection bypass.

Spec §5.1 + codex rounds 4–6: 21 explicit ``EVIL_STUBS`` covering the
known bypass shapes — direct/aliased/dynamic SQLAlchemy imports,
``getattr``-with-string, ``__import__`` chain,
``importlib.import_module``, ``operator.attrgetter``,
``__getattribute__``, ``vars`` / ``globals`` / ``locals``,
``eval`` / ``exec`` / ``compile``, ``sys.modules`` subscript, and the
``().__class__.__mro__[1].__subclasses__()`` sandbox-escape pattern.

Each stub is a tiny module string (not a real file) that the scanner
loads and rejects. If a future bypass class is discovered, adding it
to the stub list and to the scanner's symbol table closes it without
changing the scanner architecture.
"""

from __future__ import annotations

import pytest

from aslan_core.dashboard._static_sql_scan import scan

# Stubs that are forbidden in NON-queries.py modules (every dashboard
# module other than ``queries.py`` is forbidden from importing the
# SQLAlchemy expression API). These four shapes are the canonical
# legitimate ways to use ``sqlalchemy.text`` in queries.py — they
# pass the queries.py-mode scan because the call's first argument is
# a string literal (``"X"``) with no forbidden substring.
NON_QUERIES_ONLY_STUBS: list[tuple[str, str]] = [
    ("from-import", 'from sqlalchemy import text\ntext("X")'),
    ("import-as", 'import sqlalchemy as foo\nfoo.text("X")'),
    (
        "assign-alias-chain",
        "import sqlalchemy\nsa = sqlalchemy\nsa.text('X')",
    ),
    ("import-as-rebind", 'from sqlalchemy import text as t\nt("X")'),
]

# Stubs that are forbidden in EVERY mode — runtime-introspection,
# dynamic-import, code-bypass, sandbox-escape primitives have no
# legitimate use in static-SQL code regardless of file.
ALWAYS_FORBIDDEN_STUBS: list[tuple[str, str]] = [
    # ── getattr-string + dynamic resolution ──────────────────────
    (
        "getattr-string",
        'import sqlalchemy\ngetattr(sqlalchemy, "text")("X")',
    ),
    # ── Expression-API beyond text ───────────────────────────────
    (
        "select-with-for-update",
        "from sqlalchemy import select\nselect().with_for_update()",
    ),
    (
        "alias-select-with-for-update",
        "import sqlalchemy as sa\nsa.select().with_for_update()",
    ),
    (
        "func-pg-advisory-lock",
        "from sqlalchemy import func\nfunc.pg_advisory_lock(1)",
    ),
    (
        "import-sa-func-as",
        "import sqlalchemy.func as fn\nfn.pg_advisory_lock(1)",
    ),
    # ── Driver-API bypass ────────────────────────────────────────
    ("getattr-exec-driver-sql", 'getattr(conn, "exec_driver_sql")("X")'),
    # ── Dynamic import ───────────────────────────────────────────
    ("dunder-import", '__import__("sqlalchemy").text("X")'),
    (
        "importlib-import-module",
        'import importlib\nimportlib.import_module("sqlalchemy").text("X")',
    ),
    # ── Codex round-6: Python runtime escape hatches ─────────────
    (
        "sys-modules-subscript",
        'import sys\nsys.modules["sqlalchemy"].text("X")',
    ),
    (
        "vars-cache",
        'import sqlalchemy\nvars(sqlalchemy)["text"]("X")',
    ),
    (
        "operator-attrgetter",
        "import sqlalchemy, operator\noperator.attrgetter('text')(sqlalchemy)('X')",
    ),
    (
        "dunder-getattribute",
        "import sqlalchemy\nsqlalchemy.__getattribute__('text')('X')",
    ),
    ("eval-exec-import", "eval(\"__import__('sqlalchemy').text('X')\")"),
    ("exec-from-import", "exec(\"from sqlalchemy import text; text('X')\")"),
    (
        "compile-eval-chain",
        "fn = compile(\"__import__('sqlalchemy').text('X')\", '<x>', 'eval')\neval(fn)",
    ),
    (
        "globals-rebind",
        'import sqlalchemy\nglobals()["sa"] = sqlalchemy\nsa.text("X")',
    ),
    # ── Class-hierarchy walk (sandbox escape) ────────────────────
    ("class-mro-subclasses", "().__class__.__mro__[1].__subclasses__()"),
]

ALL_EVIL_STUBS: list[tuple[str, str]] = NON_QUERIES_ONLY_STUBS + ALWAYS_FORBIDDEN_STUBS


@pytest.mark.parametrize(
    ("name", "stub"),
    ALL_EVIL_STUBS,
    ids=[s[0] for s in ALL_EVIL_STUBS],
)
def test_evil_stub_is_rejected_in_non_queries_mode(name: str, stub: str) -> None:
    """Every bypass shape is rejected when scanning a module that is
    NOT ``queries.py``. The scanner must reject all 21 stubs from
    spec §5.1's EVIL_STUBS list."""
    findings = scan(stub, is_queries=False)
    assert findings, (
        f"scanner missed bypass shape {name!r} in non-queries mode\n"
        f"stub:\n{stub}\n"
        f"expected at least one Finding; got an empty list"
    )


@pytest.mark.parametrize(
    ("name", "stub"),
    ALWAYS_FORBIDDEN_STUBS,
    ids=[s[0] for s in ALWAYS_FORBIDDEN_STUBS],
)
def test_always_forbidden_stub_is_rejected_in_queries_mode(name: str, stub: str) -> None:
    """In queries.py mode ``sqlalchemy.text`` is permitted, but the
    runtime-introspection / dynamic-import / code-bypass / sandbox-
    escape primitives still have no legitimate use — every stub in
    ALWAYS_FORBIDDEN_STUBS is rejected."""
    findings = scan(stub, is_queries=True)
    assert findings, (
        f"scanner missed bypass shape {name!r} in queries.py mode\n"
        f"stub:\n{stub}\n"
        f"expected at least one Finding; got an empty list"
    )


@pytest.mark.parametrize(
    ("name", "stub"),
    NON_QUERIES_ONLY_STUBS,
    ids=[s[0] for s in NON_QUERIES_ONLY_STUBS],
)
def test_legitimate_text_use_passes_in_queries_mode(name: str, stub: str) -> None:
    """The 4 canonical ways to use ``sqlalchemy.text`` (direct, aliased,
    transitive, renamed) all pass in queries.py mode — they're the
    blessed shape, not bypasses. Catches an over-zealous scanner that
    would block legitimate code."""
    findings = scan(stub, is_queries=True)
    assert findings == [], (
        f"scanner falsely rejected legitimate sqlalchemy.text use {name!r}:\n"
        f"stub:\n{stub}\n"
        f"unexpected findings: {findings}"
    )


def test_benign_static_text_in_queries_mode_passes() -> None:
    """Sanity: a perfectly fine ``text(...)`` literal in queries.py
    yields no findings. Catches an over-zealous scanner that would
    block legitimate code."""
    benign = (
        "from sqlalchemy import text\n"
        "stmt = text('SELECT outbox_id FROM streams.outbox WHERE source_id = :sid')\n"
    )
    assert scan(benign, is_queries=True) == []


def test_benign_static_text_in_non_queries_mode_is_rejected() -> None:
    """The same benign import is rejected in non-queries mode — only
    queries.py may import sqlalchemy.text."""
    benign = (
        "from sqlalchemy import text\n"
        "stmt = text('SELECT outbox_id FROM streams.outbox WHERE source_id = :sid')\n"
    )
    findings = scan(benign, is_queries=False)
    assert findings, "non-queries.py modules MUST NOT import sqlalchemy.text"
