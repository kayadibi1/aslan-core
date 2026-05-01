"""Public surface for the v0.5.0 streams subsystem.

Re-exports the Pydantic event models registered in
``aslan_core.streams.events``. Producer / consumer / drainer classes
land in subsequent tasks (8+); for now this module is just the schema
surface.
"""

from __future__ import annotations

from aslan_core.streams.consumer import StreamConsumer
from aslan_core.streams.events import (
    EntityCreatedEvent,
    FilingAmendedEvent,
    FilingNewEvent,
    KnownStreamEvent,
    ObservationBatchEvent,
    StreamEntryRedactedEvent,
    StreamEvent,
)
from aslan_core.streams.heartbeat import heartbeat_active_intents
from aslan_core.streams.janitor import stream_deadletter_janitor
from aslan_core.streams.names import (
    PII_BEARING_STREAMS,
    STREAM_FOR_EVENT_KIND,
    STREAMS,
    normalize_bist_ticks_label,
)
from aslan_core.streams.outbox_drainer import drain_outbox
from aslan_core.streams.pii import redact_outbox_payload
from aslan_core.streams.producer import StreamProducer
from aslan_core.streams.redaction import (
    acquire_event_lock,
    canonical_payload_hash,
    redaction_lock_key,
    write_registry_entry,
)

__all__ = [
    "PII_BEARING_STREAMS",
    "STREAMS",
    "STREAM_FOR_EVENT_KIND",
    "EntityCreatedEvent",
    "FilingAmendedEvent",
    "FilingNewEvent",
    "KnownStreamEvent",
    "ObservationBatchEvent",
    "StreamConsumer",
    "StreamEntryRedactedEvent",
    "StreamEvent",
    "StreamProducer",
    "acquire_event_lock",
    "canonical_payload_hash",
    "drain_outbox",
    "heartbeat_active_intents",
    "normalize_bist_ticks_label",
    "redact_outbox_payload",
    "redaction_lock_key",
    "stream_deadletter_janitor",
    "write_registry_entry",
]
