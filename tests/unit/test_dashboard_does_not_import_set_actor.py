"""The dashboard package never imports ``aslan_core.audit.set_actor``.

Spec §3 + §6.2: the dashboard role lacks INSERT privilege on
``audit.events``, so a forgeable ``set_actor`` call from a request
handler would fail at the privilege layer anyway. Removing the
import at the source-code level closes a refactor-risk surface:
a future engineer cannot accidentally re-introduce a write path
by importing the symbol.

The test AST-walks every ``.py`` file under ``aslan_core.dashboard``
and asserts the symbol is absent from every Import / ImportFrom
node and every dotted attribute reference.
"""

from __future__ import annotations

import ast
from pathlib import Path

import aslan_core.dashboard

_FORBIDDEN_NAME = "set_actor"
_FORBIDDEN_QUALIFIED_PREFIX = "aslan_core.audit"


def _dashboard_root() -> Path:
    pkg_init_path = aslan_core.dashboard.__file__
    assert pkg_init_path is not None
    return Path(pkg_init_path).parent


def _iter_dashboard_py_files() -> list[Path]:
    root = _dashboard_root()
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _has_set_actor_import(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith(_FORBIDDEN_QUALIFIED_PREFIX):
                for alias in node.names:
                    if alias.name == _FORBIDDEN_NAME:
                        return True
        elif isinstance(node, ast.Attribute):
            if node.attr == _FORBIDDEN_NAME and isinstance(node.value, ast.Attribute | ast.Name):
                # Catch ``aslan_core.audit.set_actor(...)``-shape references.
                resolved = ast.unparse(node)
                if _FORBIDDEN_QUALIFIED_PREFIX in resolved:
                    return True
        elif isinstance(node, ast.Name):
            # Bare ``set_actor`` would imply someone aliased the import;
            # we already catch the alias at the ImportFrom node above.
            continue
    return False


def test_dashboard_package_does_not_import_set_actor() -> None:
    offenders: list[str] = []
    for py_file in _iter_dashboard_py_files():
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        if _has_set_actor_import(tree):
            offenders.append(str(py_file))
    assert not offenders, (
        f"aslan_core.dashboard MUST NOT import set_actor; "
        f"offenders: {offenders!r}. The dashboard role lacks INSERT on "
        "audit.events; importing the symbol opens a refactor-risk "
        "surface where a future call could land in a request handler."
    )
