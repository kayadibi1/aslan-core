"""Unit tests for the v0.5.0 canonical stream-name constants."""

from __future__ import annotations

import pytest

from aslan_core.streams.names import (
    PII_BEARING_STREAMS,
    STREAM_FOR_EVENT_KIND,
    STREAMS,
    normalize_bist_ticks_label,
)


def test_streams_dict_has_expected_canonical_entries() -> None:
    expected = {
        "aslan.kap.filings.new",
        "aslan.kap.filings.amended",
        "aslan.kap.filings.financial_report",
        "aslan.evds.observations.new",
        "aslan.tefas.observations.new",
        "aslan.tefas.nav.new",
        "aslan.bist.ticks",
        "aslan.entity.created",
    }
    assert expected.issubset(set(STREAMS))


def test_stream_for_event_kind_covers_all_known_payload_classes() -> None:
    """Every concrete StreamEvent.kind has a default stream registered."""
    expected = {
        "filing.new": "aslan.kap.filings.new",
        "filing.amended": "aslan.kap.filings.amended",
        "observation.batch": "aslan.evds.observations.new",
        "entity.created": "aslan.entity.created",
    }
    for kind, stream in expected.items():
        assert STREAM_FOR_EVENT_KIND[kind] == stream
        assert stream in STREAMS


def test_stream_entry_redacted_kind_routes_to_per_target_stream() -> None:
    """The redaction event has NO default mapping — the producer demands
    an explicit stream= kwarg pointing at the SAME stream as the target
    event.
    """
    assert "stream.entry_redacted" not in STREAM_FOR_EVENT_KIND


def test_pii_bearing_streams_is_frozenset() -> None:
    assert isinstance(PII_BEARING_STREAMS, frozenset)


def test_pii_bearing_streams_includes_filing_streams() -> None:
    """Filing payloads carry filing title (free-text) and entity_id —
    both can structurally surface PII-shaped data.
    """
    assert "aslan.kap.filings.new" in PII_BEARING_STREAMS
    assert "aslan.kap.filings.amended" in PII_BEARING_STREAMS


def test_normalize_bist_ticks_label_collapses_per_symbol() -> None:
    assert normalize_bist_ticks_label("aslan.bist.ticks.AKBNK") == "aslan.bist.ticks"
    assert normalize_bist_ticks_label("aslan.bist.ticks") == "aslan.bist.ticks"
    # Non-bist streams pass through.
    assert normalize_bist_ticks_label("aslan.kap.filings.new") == "aslan.kap.filings.new"


def test_streams_dict_is_immutable_to_callers() -> None:
    """Defensive — STREAMS is a MappingProxyType view; callers MUST NOT
    mutate it.
    """
    with pytest.raises(TypeError):
        STREAMS["banana"] = "x"  # type: ignore[index]


def test_stream_for_event_kind_is_immutable_to_callers() -> None:
    with pytest.raises(TypeError):
        STREAM_FOR_EVENT_KIND["filing.banana"] = "aslan.kap.banana"  # type: ignore[index]
