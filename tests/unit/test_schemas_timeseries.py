"""Unit tests for ``aslan_core.schemas.timeseries``.

Pydantic models + Literal type aliases for the v0.4.0 timeseries
surface. Verifies the IEEE-754 bit-preservation contract on
``ObservationIn.value`` and the JCS / NFC canonicalization rules on
``ObservationIn.payload_hash``.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aslan_core.errors import ObservationValidationError
from aslan_core.schemas.timeseries import (
    Observation,
    ObservationIn,
    Series,
    SeriesUpsertResult,
    SubjectRef,
    WriteCount,
)


def test_frequency_literal_accepts_spec_values() -> None:
    # All 13 spec values must be assignable as a Frequency.
    for f in (
        "tick",
        "1s",
        "1m",
        "5m",
        "15m",
        "30m",
        "1h",
        "1d",
        "1w",
        "1mo",
        "1q",
        "1y",
        "irregular",
    ):
        s = Series(
            series_code="x",
            source_id="kap",
            metric="m",
            frequency=f,
            unit="u",
            pii_class="none",
            restatement_basis="nominal",
        )
        assert s.frequency == f


def test_frequency_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        Series(
            series_code="x",
            source_id="kap",
            metric="m",
            frequency="weekly",
            unit="u",
            pii_class="none",
            restatement_basis="nominal",
        )


def test_observation_in_requires_tz_aware() -> None:
    naive = datetime(2026, 4, 29, 12, 0)  # noqa: DTZ001 -- intentionally naive for negative test
    with pytest.raises(ValidationError, match=r"tz-aware|UTC"):
        ObservationIn(ts=naive, as_of=datetime.now(UTC), value=1.0)


def test_observation_in_requires_value_or_value_text() -> None:
    with pytest.raises(ValidationError, match="value or value_text"):
        ObservationIn(ts=datetime.now(UTC), as_of=datetime.now(UTC))


def test_observation_in_rejects_both_value_and_value_text() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        ObservationIn(
            ts=datetime.now(UTC),
            as_of=datetime.now(UTC),
            value=1.0,
            value_text="AAA",
        )


def test_payload_hash_dict_key_reordering_identical() -> None:
    o1 = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 2, tzinfo=UTC),
        value=1.5,
        metadata={"a": 1, "b": 2},
    )
    o2 = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 2, tzinfo=UTC),
        value=1.5,
        metadata={"b": 2, "a": 1},
    )
    assert o1.payload_hash() == o2.payload_hash()


def test_payload_hash_signed_zero_distinct() -> None:
    """Codex F9, round 4: +0.0 and -0.0 hash to DIFFERENT bytes.
    The Observation value model is bitwise IEEE-754; sign-of-zero is
    meaningful (e.g., signed fund flows)."""
    o_pos = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        value=0.0,
    )
    o_neg = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        value=-0.0,
    )
    assert o_pos.payload_hash() != o_neg.payload_hash()


def test_payload_hash_same_nan_pattern_identical() -> None:
    """Two NaN observations with the SAME bit pattern hash identically."""
    nan_bytes = bytes.fromhex("000000000000f87f")  # canonical qNaN, LE
    n1 = struct.unpack("<d", nan_bytes)[0]
    n2 = struct.unpack("<d", nan_bytes)[0]
    o1 = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        value=n1,
    )
    o2 = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        value=n2,
    )
    assert o1.payload_hash() == o2.payload_hash()


def test_payload_hash_different_nan_patterns_differ() -> None:
    """qNaN vs sNaN bit patterns produce different hashes."""
    qnan_bytes = bytes.fromhex("000000000000f87f")  # canonical qNaN
    snan_bytes = bytes.fromhex("010000000000f0ff")  # signaling NaN
    q = struct.unpack("<d", qnan_bytes)[0]
    s = struct.unpack("<d", snan_bytes)[0]
    o_q = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        value=q,
    )
    o_s = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        value=s,
    )
    assert o_q.payload_hash() != o_s.payload_hash()


def test_metadata_with_nan_raises_before_hashing() -> None:
    """Codex F8, round 4: NaN in metadata is unrepresentable in JSON,
    so it raises ObservationValidationError BEFORE hashing — never silently
    coerced."""
    with pytest.raises(ObservationValidationError, match="finite"):
        ObservationIn(
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
            value=1.0,
            metadata={"flag": float("nan")},
        ).payload_hash()


def test_metadata_with_inf_raises_before_hashing() -> None:
    with pytest.raises(ObservationValidationError, match="finite"):
        ObservationIn(
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
            value=1.0,
            metadata={"flag": float("inf")},
        ).payload_hash()


def test_value_inf_is_allowed() -> None:
    """+inf and -inf are valid IEEE-754 — allowed in `value` field
    (some series use them as no-observation markers)."""
    o = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        value=float("inf"),
    )
    h = o.payload_hash()  # must not raise
    assert isinstance(h, str) and len(h) == 64  # sha256 hex


def test_metadata_nfc_normalisation_strings() -> None:
    """NFC vs NFD strings hash identically post-normalisation."""
    nfc = "é"  # é as a single codepoint (U+00E9)
    nfd = "é"  # e + combining acute (NFD)
    assert nfc != nfd  # raw bytes differ
    o_nfc = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        value=1.0,
        metadata={"label": nfc},
    )
    o_nfd = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        value=1.0,
        metadata={"label": nfd},
    )
    assert o_nfc.payload_hash() == o_nfd.payload_hash()


def test_subnormal_value_round_trips_in_hash() -> None:
    """Smallest positive subnormal: 0x0000000000000001."""
    subnormal_bytes = bytes.fromhex("0100000000000000")  # LE
    sn = struct.unpack("<d", subnormal_bytes)[0]
    o1 = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        value=sn,
    )
    o2 = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        value=sn,
    )
    assert o1.payload_hash() == o2.payload_hash()


def test_write_count_immutable_and_zero_default() -> None:
    wc = WriteCount(attempted=0, inserted=0, updated=0, unchanged=0)
    assert wc.attempted == 0
    # Frozen Pydantic models raise ValidationError on assignment. The
    # ``# type: ignore[misc]`` is required because the pydantic mypy
    # plugin types ``attempted`` as read-only (Final) once frozen=True.
    with pytest.raises((ValidationError, AttributeError)):
        wc.attempted = 5  # type: ignore[misc]


def test_series_upsert_result_shape() -> None:
    r = SeriesUpsertResult(series_id=42, created=True)
    assert r.series_id == 42 and r.created is True


def test_subject_ref_role_check() -> None:
    s = SubjectRef(subject_id="kap-person-001", role="executive")
    assert s.role == "executive"
    with pytest.raises(ValidationError):
        SubjectRef(subject_id="x", role="alien")


def test_pii_class_literal() -> None:
    for c in ("none", "pseudonymous", "identifying"):
        Series(
            series_code="x",
            source_id="kap",
            metric="m",
            frequency="1d",
            unit="u",
            pii_class=c,
            restatement_basis="nominal",
        )
    with pytest.raises(ValidationError):
        Series(
            series_code="x",
            source_id="kap",
            metric="m",
            frequency="1d",
            unit="u",
            pii_class="public",
            restatement_basis="nominal",
        )


def test_observation_readback_shape() -> None:
    """Observation read-back model accepts a typical row shape."""
    o = Observation(
        series_id=1,
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 2, tzinfo=UTC),
        value=1.0,
        value_text=None,
        quality_flag=0,
        ingestion_run_id=42,
        metadata={"k": "v"},
    )
    assert o.series_id == 1
    assert o.value == 1.0


def test_observation_in_payload_hash_is_64_hex() -> None:
    o = ObservationIn(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        value=42.0,
    )
    h = o.payload_hash()
    assert isinstance(h, str)
    assert len(h) == 64
    int(h, 16)  # must parse as hex
