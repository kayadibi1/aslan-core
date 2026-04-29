from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from aslan_core.audit import (
    Actor,
    current_actor,
    pop_actor,
    push_actor,
    require_actor,
    set_actor,
)
from aslan_core.errors import AuditMissingActor


def test_actor_immutable_and_validates_kind() -> None:
    a = Actor(actor_id="user:sidar@aslan.ai", actor_kind="user")
    assert a.actor_id == "user:sidar@aslan.ai"
    assert a.actor_kind == "user"
    # Frozen dataclass — assignment raises FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        a.actor_id = "other"  # type: ignore[misc]
    with pytest.raises(ValueError, match="actor_kind"):
        Actor(actor_id="x", actor_kind="alien")  # type: ignore[arg-type]


def test_actor_with_optional_fields() -> None:
    rid = uuid4()
    a = Actor(
        actor_id="service:kap-scraper",
        actor_kind="service",
        client_ip="10.0.0.1",
        user_agent="aslan-scraper/0.4.2",
        request_id=rid,
    )
    assert a.client_ip == "10.0.0.1"
    assert a.request_id == rid


def test_actor_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="actor_id"):
        Actor(actor_id="", actor_kind="user")


def test_set_get_clear_actor() -> None:
    assert current_actor() is None
    a = Actor(actor_id="user:a", actor_kind="user")
    set_actor(a)
    try:
        assert current_actor() == a
    finally:
        set_actor(None)
    assert current_actor() is None


def test_require_actor_raises_when_unset() -> None:
    set_actor(None)
    with pytest.raises(AuditMissingActor):
        require_actor()


def test_require_actor_returns_set_actor() -> None:
    a = Actor(actor_id="user:a", actor_kind="user")
    set_actor(a)
    try:
        assert require_actor() == a
    finally:
        set_actor(None)


@pytest.mark.asyncio(loop_scope="session")
async def test_actor_propagates_into_async_subtask() -> None:
    """asyncio.create_task copies the current ContextVar; the subtask sees
    the actor set by the parent. Mutations inside subtasks should NOT need
    to re-set the actor."""
    set_actor(Actor(actor_id="user:parent", actor_kind="user"))
    try:
        captured: list[Actor | None] = []

        async def child() -> None:
            captured.append(current_actor())

        await asyncio.create_task(child())
        assert captured[0] is not None
        assert captured[0].actor_id == "user:parent"
    finally:
        set_actor(None)


def test_push_pop_actor_restores_previous_value() -> None:
    """push_actor returns a Token that, when passed to pop_actor,
    restores the ContextVar to its prior state — even if that prior
    state was a different Actor (nested scopes) or None (unscoped)."""
    assert current_actor() is None
    outer = Actor(actor_id="user:outer", actor_kind="user")
    inner = Actor(actor_id="user:inner", actor_kind="user")

    t_outer = push_actor(outer)
    try:
        assert current_actor() == outer
        t_inner = push_actor(inner)
        try:
            assert current_actor() == inner
        finally:
            pop_actor(t_inner)
        assert current_actor() == outer
    finally:
        pop_actor(t_outer)
    assert current_actor() is None


@pytest.mark.asyncio(loop_scope="session")
async def test_actor_change_in_subtask_does_not_leak_to_parent() -> None:
    """Subtask sees a copy of the parent's ContextVar. Changes in the
    subtask do not propagate back. This makes per-request actor scoping
    safe."""
    parent_actor = Actor(actor_id="user:parent", actor_kind="user")
    set_actor(parent_actor)
    try:

        async def child() -> None:
            set_actor(Actor(actor_id="user:child", actor_kind="user"))

        await asyncio.create_task(child())
        after = current_actor()
        assert after is not None
        assert after.actor_id == "user:parent"
    finally:
        set_actor(None)
