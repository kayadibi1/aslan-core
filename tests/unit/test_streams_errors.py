"""Unit tests for the v0.5.0 stream-error subtree (plan Task 2)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from aslan_core.errors import (
    AslanCoreError,
    StreamConsumerLagExceeded,
    StreamError,
    StreamEventIdConflict,
    StreamPayloadValidationError,
    StreamRoutingContention,
    StreamSchemaVersionMismatch,
    UnknownEventKind,
)


def test_stream_error_subclass_of_aslan_core_error() -> None:
    assert issubclass(StreamError, AslanCoreError)


def test_stream_schema_version_mismatch_carries_diagnostic_fields() -> None:
    eid = uuid4()
    err = StreamSchemaVersionMismatch(
        event_id=eid,
        stream_name="aslan.kap.filings.new",
        received_version=2,
        supported_range=(1, 1),
        direction="newer",
    )
    assert err.event_id == eid
    assert err.stream_name == "aslan.kap.filings.new"
    assert err.received_version == 2
    assert err.supported_range == (1, 1)
    assert err.direction == "newer"
    # The string form must be operator-readable.
    assert "schema_version=2" in str(err)
    assert "newer" in str(err)


def test_stream_schema_version_mismatch_direction_older() -> None:
    err = StreamSchemaVersionMismatch(
        event_id=uuid4(),
        stream_name="x",
        received_version=0,
        supported_range=(1, 3),
        direction="older",
    )
    assert err.direction == "older"


def test_stream_payload_validation_error_carries_event_id_and_message() -> None:
    eid = uuid4()
    err = StreamPayloadValidationError(
        event_id=eid,
        stream_name="x",
        message="unknown discriminator 'filing.banana'",
    )
    assert err.event_id == eid
    assert "banana" in str(err)


def test_stream_payload_validation_error_event_id_may_be_none() -> None:
    """Pre-discriminator failure (malformed JSON / missing ``event_id``)
    has no event_id to carry."""
    err = StreamPayloadValidationError(
        event_id=None,
        stream_name="x",
        message="no event_id field",
    )
    assert err.event_id is None
    assert "no event_id" in str(err)


def test_stream_consumer_lag_exceeded_carries_lag_seconds() -> None:
    err = StreamConsumerLagExceeded(
        stream_name="x",
        group_name="g",
        lag_seconds=900.0,
    )
    assert err.lag_seconds == 900.0


def test_stream_event_id_conflict_carries_event_id_and_stream() -> None:
    eid = uuid4()
    err = StreamEventIdConflict(event_id=eid, stream_name="x")
    assert err.event_id == eid
    assert err.stream_name == "x"


def test_unknown_event_kind_carries_kind_string() -> None:
    err = UnknownEventKind(kind="filing.unknown")
    assert "filing.unknown" in str(err)


def test_stream_routing_contention_codex_f22() -> None:
    """Codex F22 round 11+12 — adoption looped past the 3-attempt cap."""
    err = StreamRoutingContention(failure_id=42, attempts=4)
    assert err.failure_id == 42
    assert err.attempts == 4
    assert "42" in str(err)


def test_all_new_stream_errors_subclass_stream_error() -> None:
    """Every new error inherits from StreamError so a single ``except
    StreamError`` clause catches the whole subtree."""
    for cls in (
        StreamSchemaVersionMismatch,
        StreamPayloadValidationError,
        StreamConsumerLagExceeded,
        StreamEventIdConflict,
        UnknownEventKind,
        StreamRoutingContention,
    ):
        assert issubclass(cls, StreamError), f"{cls.__name__} does not subclass StreamError"


def test_existing_stream_errors_unchanged() -> None:
    """Backward-compat: the v0.3-era StreamPublishError / StreamReadError
    / StreamDeserializeError stubs still subclass StreamError so callers
    that already catch them keep working."""
    from aslan_core.errors import (
        StreamDeserializeError,
        StreamPublishError,
        StreamReadError,
    )

    assert issubclass(StreamPublishError, StreamError)
    assert issubclass(StreamReadError, StreamError)
    assert issubclass(StreamDeserializeError, StreamError)


def test_stream_consumer_lag_exceeded_message_format() -> None:
    err = StreamConsumerLagExceeded(
        stream_name="aslan.kap.filings.new",
        group_name="kap-tail",
        lag_seconds=42.5,
    )
    s = str(err)
    assert "aslan.kap.filings.new" in s
    assert "kap-tail" in s
    assert "42.5" in s


def test_stream_event_id_conflict_message_includes_id() -> None:
    eid = uuid4()
    err = StreamEventIdConflict(event_id=eid, stream_name="streamX")
    s = str(err)
    assert str(eid) in s
    assert "streamX" in s


def test_unknown_event_kind_message_mentions_remediation() -> None:
    err = UnknownEventKind(kind="banana.split")
    s = str(err)
    # Operators reading the error should know what to do.
    assert "STREAM_FOR_EVENT_KIND" in s or "stream=" in s


@pytest.mark.parametrize("attempts", [4, 5, 99])
def test_stream_routing_contention_carries_attempts(attempts: int) -> None:
    err = StreamRoutingContention(failure_id=7, attempts=attempts)
    assert err.attempts == attempts
    assert str(attempts) in str(err)
