"""Pytest fixtures shared across all integration tests.

Adds the autouse ``_default_test_actor`` fixture so existing tests
that don't care about audit identity don't need to set one
themselves. Tests that exercise the audit-strict-without-actor path
(``tests/integration/test_strict_actor_enforcement.py``) opt out via
a local fixture that re-clears the ContextVar after this autouse
runs.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from aslan_core.audit import Actor, set_actor


@pytest.fixture(autouse=True)
def _default_test_actor() -> Iterator[None]:
    """Set a default test Actor for any mutation that fires during
    integration tests.

    Codex F3, 2026-04-29: this fixture is convenient for the existing
    100+ tests that do not care about audit, but it MUST NOT mask
    the strict-mode contract. Tests that exercise the unset-actor
    path declare a local autouse fixture that runs AFTER this one
    (Pytest applies file-local autouse fixtures after conftest's),
    re-clearing the ContextVar before the test body runs. See
    ``test_strict_actor_enforcement.py`` for the canonical pattern.
    """
    set_actor(Actor(actor_id="user:pytest", actor_kind="user"))
    yield
    set_actor(None)
