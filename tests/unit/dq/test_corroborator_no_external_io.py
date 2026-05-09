"""AST canary — ``aslan_core.dq.corroborator`` never makes a real
external HTTP call without going through ``_firecrawl_fetch``.

Per NG6 contract: every external network call is funnelled through
the single ``_firecrawl_fetch`` boundary so test code can mock it.
This canary parses the module's AST and asserts:

  1. ``requests``, ``httpx``, ``urllib.request``, ``aiohttp``, and
     ``urllib3`` are NOT imported at module level (they are not
     imported anywhere — but the AST scan only sees module-level).

  2. There are no top-level / nested calls to ``open()`` over an
     ``http://`` / ``https://`` URL string literal.

  3. The only function whose body references ``subprocess.run`` is
     ``_firecrawl_via_cli`` — every other call site would be a back
     door around the firecrawl boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[3] / "src" / "aslan_core" / "dq" / "corroborator.py"


_FORBIDDEN_NETWORK_IMPORTS = frozenset(
    {
        "requests",
        "httpx",
        "aiohttp",
        "urllib.request",
        "urllib3",
        "pycurl",
    }
)


def _module_tree() -> ast.Module:
    source = _MODULE_PATH.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(_MODULE_PATH))


def test_no_forbidden_network_imports_at_module_level() -> None:
    tree = _module_tree()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in _FORBIDDEN_NETWORK_IMPORTS, (
                    f"corroborator imports forbidden network module {alias.name!r} "
                    f"at module level — every external call must go through "
                    f"_firecrawl_fetch"
                )
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert mod not in _FORBIDDEN_NETWORK_IMPORTS, (
                f"corroborator imports forbidden network module {mod!r} "
                f"at module level — every external call must go through "
                f"_firecrawl_fetch"
            )


def test_subprocess_run_only_in_firecrawl_via_cli() -> None:
    """``subprocess.run`` is the CLI shell-out path. It MUST live only
    inside ``_firecrawl_via_cli`` — any other site would be a back door
    around the firecrawl boundary.
    """
    tree = _module_tree()

    def _function_uses_subprocess_run(fn: ast.FunctionDef) -> bool:
        for sub in ast.walk(fn):
            # Match `subprocess.run(...)` — Attribute "run" over a Name
            # `subprocess`.
            if (
                isinstance(sub, ast.Attribute)
                and sub.attr == "run"
                and isinstance(sub.value, ast.Name)
                and sub.value.id == "subprocess"
            ):
                return True
        return False

    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and _function_uses_subprocess_run(node)
            and node.name != "_firecrawl_via_cli"
        ):
            offenders.append(node.name)
    assert offenders == [], (
        f"functions {offenders!r} use subprocess.run outside _firecrawl_via_cli — "
        f"every external call must go through _firecrawl_fetch"
    )


def test_no_top_level_url_open() -> None:
    """No module-level call constructs an HTTP/HTTPS request.

    Catches sneak-ins like a top-level ``urllib.request.urlopen(...)``.
    """
    tree = _module_tree()
    for node in tree.body:
        for sub in ast.walk(node):
            # The forbidden shape is something.<verb>(<str literal>).
            if not (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in ("urlopen", "get", "post", "request")
            ):
                continue
            for arg in sub.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    value = arg.value.lower()
                    assert not value.startswith(("http://", "https://")), (
                        f"corroborator has a top-level HTTP call to "
                        f"{arg.value!r} — every external call must go "
                        f"through _firecrawl_fetch"
                    )


def test_firecrawl_fetch_is_the_single_entry_point() -> None:
    """``refresh()`` calls ``_firecrawl_fetch`` exactly — not the
    SDK or CLI helpers directly."""
    tree = _module_tree()
    refresh_fn = next(
        (n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "refresh"),
        None,
    )
    assert refresh_fn is not None, "refresh() not found in corroborator module"
    found = False
    for sub in ast.walk(refresh_fn):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            if sub.func.id == "_firecrawl_fetch":
                found = True
            assert sub.func.id not in ("_firecrawl_via_cli", "_firecrawl_via_sdk"), (
                f"refresh() calls {sub.func.id} directly — must go through _firecrawl_fetch"
            )
    assert found, "refresh() does not call _firecrawl_fetch"
