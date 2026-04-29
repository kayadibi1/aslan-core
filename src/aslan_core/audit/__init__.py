"""Audit + actor-identity public API for aslan-core.

See ``plans/aslan-core-v0.3.0-audit-obs.md`` for design rationale.
"""

from __future__ import annotations

from aslan_core.audit.context import (
    Actor,
    ActorKind,
    current_actor,
    pop_actor,
    push_actor,
    require_actor,
    set_actor,
)
from aslan_core.audit.recorder import (
    AuditRecord,
    assert_actor_or_strict_raise,
    record,
)

__all__ = [
    "Actor",
    "ActorKind",
    "AuditRecord",
    "assert_actor_or_strict_raise",
    "current_actor",
    "pop_actor",
    "push_actor",
    "record",
    "require_actor",
    "set_actor",
]
