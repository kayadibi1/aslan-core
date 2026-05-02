"""v0.6.0 Task 3 — package skeleton import test.

Verifies the ``aslan_core.dashboard`` subpackage is importable and the
``serve()`` entry-point has the documented signature. Does NOT call
``serve()`` — the implementation lands in Task 12 and currently raises
``NotImplementedError``.
"""

from __future__ import annotations

import inspect


def test_dashboard_module_importable() -> None:
    from aslan_core import dashboard  # noqa: F401


def test_serve_helper_signature() -> None:
    from aslan_core.dashboard import serve

    sig = inspect.signature(serve)
    assert "host" in sig.parameters
    assert "port" in sig.parameters
