"""Actor + ContextVar plumbing for audit attribution.

See ``plans/aslan-core-v0.3.0-audit-obs.md`` §1 for design rationale.

Public surface: :class:`Actor`, :func:`set_actor`, :func:`current_actor`,
:func:`require_actor`. Asyncio's task creation copies the ContextVar
map, so subtasks inherit the parent's actor automatically and changes
inside a subtask do NOT leak back to the parent.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final, Literal
from uuid import UUID

from aslan_core.errors import AuditMissingActor

ActorKind = Literal["user", "service", "system"]
_VALID_KINDS: Final[frozenset[str]] = frozenset({"user", "service", "system"})


@dataclass(frozen=True, slots=True)
class Actor:
    """Identity captured on every aslan-core mutation.

    Set via :func:`set_actor` at the request / job boundary. Read via
    :func:`current_actor` or :func:`require_actor` from inside mutation
    code paths. Subtasks inherit the parent's actor automatically because
    Python's asyncio copies the ContextVar map at task creation.

    The ``actor_id`` convention is ``<kind>:<identifier>`` (e.g.
    ``user:sidar@aslan.ai``, ``service:kap-scraper``,
    ``system:cron-rebuild-fts``) but the prefix is not enforced here —
    consumers may adopt their own convention as long as ``actor_kind``
    is set correctly.
    """

    actor_id: str
    actor_kind: ActorKind
    client_ip: str | None = None
    user_agent: str | None = None
    request_id: UUID | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.actor_id:
            raise ValueError("actor_id must be a non-empty string")
        if self.actor_kind not in _VALID_KINDS:
            raise ValueError(
                f"actor_kind must be one of {sorted(_VALID_KINDS)}; got {self.actor_kind!r}"
            )


_current_actor: ContextVar[Actor | None] = ContextVar("aslan_current_actor", default=None)


def set_actor(actor: Actor | None) -> None:
    """Set the actor for the current async task (and its descendants).

    Pass ``None`` to clear. Asyncio's task creation copies the ContextVar
    map, so every subtask sees the actor at the moment it was created.
    Changes made inside a subtask do NOT propagate back to the parent.
    """
    _current_actor.set(actor)


def current_actor() -> Actor | None:
    """Return the actor for the current async task, or ``None`` if unset."""
    return _current_actor.get()


def require_actor() -> Actor:
    """Return the current actor or raise :class:`AuditMissingActor`."""
    a = _current_actor.get()
    if a is None:
        raise AuditMissingActor(
            "no actor set; call aslan_core.audit.set_actor(...) at the "
            "request/job boundary, or pass actor= to ingestion_run(...)"
        )
    return a
