"""Unit tests for the v0.5.0 stream event payload models.

Per plan Task 1 (codex spec §3, §4 — discriminated-union dispatch on
`kind`; frozen post-construction; tz-aware UTC enforced; producer-side
``extra="forbid"`` catches typos).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from aslan_core.streams.events import (
    EntityCreatedEvent,
    FilingAmendedEvent,
    FilingNewEvent,
    KnownStreamEvent,
    ObservationBatchEvent,
    StreamEntryRedactedEvent,
    StreamEvent,
)


def _stamps(**override: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": 1,
        "event_id": uuid4(),
        "produced_at": datetime.now(UTC),
        "producer_run_id": 42,
        "source_id": "kap",
    }
    base.update(override)
    return base


def test_stream_event_requires_tz_aware_produced_at() -> None:
    """Naive ``produced_at`` must raise — UTC tz-awareness is the v0.3
    contract."""
    with pytest.raises(ValidationError, match=r"tz-aware|UTC"):
        FilingNewEvent(
            **_stamps(produced_at=datetime(2026, 4, 29, 12, 0)),  # noqa: DTZ001 — test of naive
            filing_id=uuid4(),
            entity_id=None,
            filing_kind="material_event",
            title="t",
            published_at=datetime.now(UTC),
            primary_object_key="kap/raw/x.html",
            bucket="aslan-docs",
            is_revision=False,
            revision_no=1,
        )


def test_stream_event_frozen_post_construction() -> None:
    """``model_config = ConfigDict(frozen=True)`` — the consumer cannot
    mutate an event in flight (codex spec §3)."""
    e = FilingNewEvent(
        **_stamps(),
        filing_id=uuid4(),
        entity_id=None,
        filing_kind="material_event",
        title="t",
        published_at=datetime.now(UTC),
        primary_object_key="k",
        bucket="b",
        is_revision=False,
        revision_no=1,
    )
    with pytest.raises(ValidationError):
        e.title = "mutated"  # type: ignore[misc]


def test_stream_event_extra_forbid_on_producer_side() -> None:
    """Producer-side: an unknown field must raise — catches typos /
    drift against the discriminated-union schema."""
    with pytest.raises(ValidationError, match=r"[Ee]xtra"):
        FilingNewEvent(
            **_stamps(),
            filing_id=uuid4(),
            entity_id=None,
            filing_kind="material_event",
            title="t",
            published_at=datetime.now(UTC),
            primary_object_key="k",
            bucket="b",
            is_revision=False,
            revision_no=1,
            unknown_field="oops",  # type: ignore[call-arg]
        )


def test_known_stream_event_discriminator_dispatch() -> None:
    """``KnownStreamEvent`` parses by ``kind`` discriminator, returning
    the correct concrete subclass."""
    payload = {
        **_stamps(event_id=uuid4()),
        "kind": "filing.new",
        "filing_id": str(uuid4()),
        "entity_id": None,
        "filing_kind": "material_event",
        "title": "t",
        "published_at": datetime.now(UTC).isoformat(),
        "primary_object_key": "k",
        "bucket": "b",
        "is_revision": False,
        "revision_no": 1,
    }
    payload["produced_at"] = datetime.now(UTC).isoformat()
    payload["event_id"] = str(payload["event_id"])
    parsed: KnownStreamEvent = TypeAdapter(KnownStreamEvent).validate_python(payload)
    assert isinstance(parsed, FilingNewEvent)
    assert parsed.kind == "filing.new"


def test_known_stream_event_unknown_kind_raises() -> None:
    """Unknown ``kind`` -> ValidationError; the consumer's failure
    handler routes to deadletter via ``StreamPayloadValidationError``
    (Task 14)."""
    bad = _stamps()
    bad["kind"] = "filing.unknown"  # not in the union
    with pytest.raises(ValidationError, match=r"discriminator|literal|tag"):
        TypeAdapter(KnownStreamEvent).validate_python(bad)


def test_observation_batch_event_round_trip() -> None:
    e = ObservationBatchEvent(
        **_stamps(source_id="evds"),
        series_code="USD/TRY",
        series_id=7,
        ts_min=datetime(2026, 1, 1, tzinfo=UTC),
        ts_max=datetime(2026, 1, 2, tzinfo=UTC),
        as_of_min=datetime(2026, 1, 3, tzinfo=UTC),
        as_of_max=datetime(2026, 1, 3, tzinfo=UTC),
        n_observations=42,
        batch_payload_hash="a" * 64,
    )
    dumped = e.model_dump(mode="json")
    assert dumped["kind"] == "observation.batch"
    parsed: KnownStreamEvent = TypeAdapter(KnownStreamEvent).validate_python(dumped)
    assert isinstance(parsed, ObservationBatchEvent)
    assert parsed.series_code == "USD/TRY"


def test_entity_created_event_minimal() -> None:
    e = EntityCreatedEvent(
        **_stamps(source_id="kap"),
        entity_id=uuid4(),
        entity_type="company",
        legal_name="Akbank T.A.S.",
        primary_identifier_namespace="bist:mkk",
        primary_identifier_value="AKBNK",
    )
    assert e.kind == "entity.created"


def test_filing_amended_carries_previous_filing_id() -> None:
    e = FilingAmendedEvent(
        **_stamps(),
        filing_id=uuid4(),
        previous_filing_id=uuid4(),
        entity_id=uuid4(),
        revision_no=2,
        title="Q1 (revised)",
        published_at=datetime.now(UTC),
    )
    assert e.kind == "filing.amended"
    assert e.previous_filing_id is not None


def test_actor_optional_when_not_strict() -> None:
    """Auto-stamp leaves ``actor_id`` None when ``current_actor()`` is
    None and ``audit_strict=False``; the producer fills it later (Task
    8)."""
    e = FilingNewEvent(
        **_stamps(),
        filing_id=uuid4(),
        entity_id=None,
        filing_kind="material_event",
        title="t",
        published_at=datetime.now(UTC),
        primary_object_key="k",
        bucket="b",
        is_revision=False,
        revision_no=1,
    )
    assert e.actor_id is None
    assert e.actor_kind is None


def test_stream_entry_redacted_event_kind() -> None:
    """The redaction-notification event is part of the discriminated
    union so consumers can opt-in to scrub-on-redact behavior (Task
    18)."""
    e = StreamEntryRedactedEvent(
        **_stamps(),
        target_event_id=uuid4(),
        target_stream="aslan.kap.filings.new",
        redaction_reason="Art.17",
    )
    assert e.kind == "stream.entry_redacted"
    parsed: KnownStreamEvent = TypeAdapter(KnownStreamEvent).validate_python(
        e.model_dump(mode="json")
    )
    assert isinstance(parsed, StreamEntryRedactedEvent)


def test_stream_event_base_class_exists() -> None:
    """``StreamEvent`` is the abstract-ish base every concrete payload
    inherits from; consumers may use it as an isinstance bound."""
    e = FilingNewEvent(
        **_stamps(),
        filing_id=uuid4(),
        entity_id=None,
        filing_kind="material_event",
        title="t",
        published_at=datetime.now(UTC),
        primary_object_key="k",
        bucket="b",
        is_revision=False,
        revision_no=1,
    )
    assert isinstance(e, StreamEvent)
