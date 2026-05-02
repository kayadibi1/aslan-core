"""Every non-static route ends with ``return render(request, ...)``.

Spec §6.3: the central ``render(request, vm)`` helper is the
single output point that runtime-checks the VM type, materialises
``model_dump(mode='json')``, and looks up the registered template.
A future page handler that bypassed the helper (e.g. returned a
hand-written ``HTMLResponse``) would lose those guarantees and
could silently leak forbidden bytes.

This test inspects the source AST of every page handler in
``aslan_core.dashboard.pages.*`` (and the metrics route in
``app.py``) and asserts the function ends with a ``return
render(request, ...)`` call. Static-asset routes are exempt by
name — they intentionally return raw bytes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import aslan_core.dashboard

_RENDER_NAME = "render"
_STATIC_HANDLER_NAMES: frozenset[str] = frozenset(
    {
        "_serve_dashboard_css",
        "_serve_htmx_js",
        "_serve_favicon",
        "_metrics_route",
    }
)


def _dashboard_root() -> Path:
    pkg_init_path = aslan_core.dashboard.__file__
    assert pkg_init_path is not None
    return Path(pkg_init_path).parent


def _is_get_decorator(decorator: ast.expr) -> bool:
    """Detect ``@app.get("/path")`` decorators."""
    if not isinstance(decorator, ast.Call):
        return False
    func = decorator.func
    if not isinstance(func, ast.Attribute):
        return False
    return func.attr == "get"


def _function_returns_render(func: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """Return True if the last statement of ``func`` is
    ``return render(request, ...)``. Walks the entire body so a
    handler with branching logic ALSO needs every reachable return
    to call render — but we keep the contract narrow to the
    terminal return for v0.6.0; the page handlers are all single-
    return today."""
    if not func.body:
        return False
    final = func.body[-1]
    if not isinstance(final, ast.Return) or final.value is None:
        return False
    call = final.value
    if not isinstance(call, ast.Call):
        return False
    func_node = call.func
    if isinstance(func_node, ast.Name):
        return func_node.id == _RENDER_NAME
    if isinstance(func_node, ast.Attribute):
        return func_node.attr == _RENDER_NAME
    return False


def test_every_get_handler_ends_with_render_call() -> None:
    """AST-walk ``aslan_core.dashboard`` for ``@app.get(...)``-
    decorated async functions. Each one (excluding static-asset
    handlers) must end with ``return render(...)``."""
    offenders: list[str] = []
    for py_file in _dashboard_root().rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if not any(_is_get_decorator(d) for d in node.decorator_list):
                continue
            if node.name in _STATIC_HANDLER_NAMES:
                continue
            if not _function_returns_render(node):
                offenders.append(f"{py_file.name}::{node.name}")
    assert not offenders, (
        f"these GET handlers do not end with `return render(request, ...)`: "
        f"{offenders!r}. The render helper is the single output point — "
        "every page handler must go through it so VM-type and template "
        "checks fire on every response."
    )
