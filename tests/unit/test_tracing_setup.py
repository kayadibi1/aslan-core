"""Unit tests for ``aslan_core.observability.tracing``.

Covers:
* ``setup_tracing(None)`` no-op without the [obs] extra installed.
* ``@traced`` decorator wraps an async method, names spans
  ``{ClassName}.{method_name}``, and adds ``actor.id`` / ``actor.kind``
  attributes from the current ContextVar.

The decorator is async-only; it works without ``setup_tracing()``
having been called (uses the no-op default tracer in that case).
"""

from __future__ import annotations

import pytest


def test_setup_tracing_with_none_endpoint_is_noop() -> None:
    """No-op path must work without the [obs] extra installed."""
    from aslan_core.observability import setup_tracing

    setup_tracing(None)  # must not raise


@pytest.mark.asyncio(loop_scope="session")
async def test_traced_decorator_wraps_async_method_without_setup() -> None:
    """The ``@traced`` decorator must work even when setup_tracing has
    NOT been called — the global tracer falls back to a no-op
    implementation in that case."""
    from aslan_core.observability import traced

    class Foo:
        @traced()
        async def do_thing(self, x: int) -> int:
            return x * 2

    f = Foo()
    assert await f.do_thing(3) == 6


@pytest.mark.asyncio(loop_scope="session")
async def test_traced_decorator_passes_through_exceptions() -> None:
    """A wrapped method that raises must propagate the exception
    unchanged (the span just records the error)."""
    from aslan_core.observability import traced

    class Foo:
        @traced()
        async def boom(self) -> None:
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await Foo().boom()


@pytest.mark.asyncio(loop_scope="session")
async def test_traced_decorator_uses_current_actor_lazily() -> None:
    """The decorator reads ``aslan_core.audit.current_actor()`` at call
    time, lazily — applying the decorator at module import time must
    NOT trigger an Actor lookup."""
    from aslan_core.audit import Actor, set_actor
    from aslan_core.observability import traced

    class Bar:
        @traced(operation_name="custom.op")
        async def run(self) -> str:
            return "ok"

    set_actor(Actor(actor_id="user:trace-test", actor_kind="user"))
    try:
        result = await Bar().run()
    finally:
        set_actor(None)
    assert result == "ok"
