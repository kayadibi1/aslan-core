"""Pydantic event payload models for aslan-core streams.

Codex spec §3, §4 — discriminated-union dispatch on ``kind``; frozen
post-construction; tz-aware UTC enforced; producer-side
``extra="forbid"`` catches typos. Consumer-side subclasses override
with ``extra="ignore"`` in Task 12 to give us additive-minor
schema-version forward-compat.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StreamEvent(BaseModel):
    """Base class for every event published through ``aslan_core.streams``."""

    schema_version: int
    event_id: UUID
    produced_at: datetime
    producer_run_id: int
    source_id: str
    actor_id: str | None = None
    actor_kind: Literal["user", "service", "system"] | None = None
    traceparent: str | None = None
    request_id: UUID | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("produced_at")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("produced_at must be tz-aware UTC")
        return v


class FilingNewEvent(StreamEvent):
    kind: Literal["filing.new"] = "filing.new"
    filing_id: UUID
    entity_id: UUID | None
    filing_kind: str
    title: str
    published_at: datetime
    primary_object_key: str
    bucket: str
    is_revision: bool
    revision_no: int


class FilingAmendedEvent(StreamEvent):
    kind: Literal["filing.amended"] = "filing.amended"
    filing_id: UUID
    previous_filing_id: UUID
    entity_id: UUID | None
    revision_no: int
    title: str
    published_at: datetime


class ObservationBatchEvent(StreamEvent):
    kind: Literal["observation.batch"] = "observation.batch"
    series_code: str
    series_id: int
    ts_min: datetime
    ts_max: datetime
    as_of_min: datetime
    as_of_max: datetime
    n_observations: int
    batch_payload_hash: str


class EntityCreatedEvent(StreamEvent):
    kind: Literal["entity.created"] = "entity.created"
    entity_id: UUID
    entity_type: str
    legal_name: str
    primary_identifier_namespace: str | None
    primary_identifier_value: str | None


class StreamEntryRedactedEvent(StreamEvent):
    """GDPR Art. 17 redaction-completed notification (Task 18).

    Carries the ``event_id`` of the redacted target event and the
    original stream name; ``redaction_reason`` is bounded ('Art.17',
    future 'Art.16', etc.).
    """

    kind: Literal["stream.entry_redacted"] = "stream.entry_redacted"
    target_event_id: UUID
    target_stream: str
    redaction_reason: str


KnownStreamEvent = Annotated[
    FilingNewEvent
    | FilingAmendedEvent
    | ObservationBatchEvent
    | EntityCreatedEvent
    | StreamEntryRedactedEvent,
    Field(discriminator="kind"),
]


__all__ = [
    "EntityCreatedEvent",
    "FilingAmendedEvent",
    "FilingNewEvent",
    "KnownStreamEvent",
    "ObservationBatchEvent",
    "StreamEntryRedactedEvent",
    "StreamEvent",
]
