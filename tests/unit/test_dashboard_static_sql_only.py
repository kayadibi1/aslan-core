"""Every dashboard module other than ``queries.py`` is scanner-clean.

Spec §5.1: ``queries.py`` is the ONLY module in
``aslan_core.dashboard`` that may construct SQL. Every other module
imports query helpers by name. The scanner walks each module's AST
and asserts no findings — no SQLAlchemy expression-API symbol, no
runtime-introspection, no dynamic-import / code-bypass primitive.

Catches a future refactor that puts ``from sqlalchemy import text``
into a page module, an htmx partial, or a render helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aslan_core.dashboard._static_sql_scan import scan

_DASHBOARD_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "aslan_core" / "dashboard"
_QUERIES_FILENAME = "queries.py"
_SCANNER_FILENAME = "_static_sql_scan.py"


def _enumerate_non_queries_modules() -> list[Path]:
    return [
        p
        for p in sorted(_DASHBOARD_PACKAGE.rglob("*.py"))
        if p.name not in {_QUERIES_FILENAME, _SCANNER_FILENAME}
    ]


@pytest.mark.parametrize(
    "module_path",
    _enumerate_non_queries_modules(),
    ids=lambda p: str(p.relative_to(_DASHBOARD_PACKAGE)),
)
def test_dashboard_module_is_scanner_clean(module_path: Path) -> None:
    source = module_path.read_text(encoding="utf-8")
    findings = scan(source, is_queries=False)
    assert findings == [], (
        f"{module_path.relative_to(_DASHBOARD_PACKAGE)} produced findings: {findings}"
    )


def test_only_queries_py_imports_sqlalchemy_text() -> None:
    """Belt-and-suspenders: explicit string-based check that the only
    file in ``aslan_core.dashboard`` containing ``import.*text`` from
    ``sqlalchemy`` is ``queries.py``. Catches a typo that snuck the
    import past the AST scanner."""
    offenders: list[str] = []
    for path in _DASHBOARD_PACKAGE.rglob("*.py"):
        if path.name in {_QUERIES_FILENAME, _SCANNER_FILENAME}:
            continue
        source = path.read_text(encoding="utf-8")
        if "from sqlalchemy" in source or "import sqlalchemy" in source:
            offenders.append(str(path.relative_to(_DASHBOARD_PACKAGE)))
    assert not offenders, f"only queries.py may import sqlalchemy; offenders: {offenders}"
