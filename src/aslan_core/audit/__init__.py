"""Audit + actor-identity public API for aslan-core.

See ``plans/aslan-core-v0.3.0-audit-obs.md`` for design rationale.
"""

from __future__ import annotations

from aslan_core.audit.context import (
    Actor,
    ActorKind,
    current_actor,
    require_actor,
    set_actor,
)
from aslan_core.audit.recorder import AuditRecord, record

__all__ = [
    "Actor",
    "ActorKind",
    "AuditRecord",
    "current_actor",
    "record",
    "require_actor",
    "set_actor",
]
