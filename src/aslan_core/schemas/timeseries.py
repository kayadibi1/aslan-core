"""Pydantic models for the v0.4.0 timeseries surface.

Public re-exports live in ``aslan_core.schemas`` (and through the
top-level package ``aslan_core``). ORM models in ``aslan_core.models.ts``
are NOT public API.

Codex F10 contract: ``ObservationIn.value: float | None`` is bare —
NO custom validators that mutate the IEEE-754 bit pattern. Pydantic
v2's default float coercion is bit-preserving for an in-memory
``float`` (Python's ``float`` is C ``double`` / IEEE-754 binary64),
which is exactly what the round-trip-byte-equality contract requires.

Codex F8/F9 contract: ``payload_hash`` splits the canonicalisation —
- ``value``: 8 bytes of IEEE-754 binary64, big-endian. Sign-of-zero
  preserved. NaN bit pattern preserved. Subnormals preserved.
- ``metadata``: RFC 8785 (JCS)-style canonical JSON with sorted keys
  and NFC-normalised UTF-8 strings. Finite-floats-only is enforced
  BEFORE hashing — NaN / Inf in metadata raises
  ``ObservationValidationError`` (JSON cannot represent them, allowing
  them creates a hash-vs-storage divergence).
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import unicodedata
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aslan_core.errors import ObservationValidationError

Frequency = Literal[
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
]
RestatementBasis = Literal["nominal", "as_reported", "restated", "adjusted"]
PiiClass = Literal["none", "pseudonymous", "identifying"]
SubjectRole = Literal[
    "data_subject",
    "reporter",
    "beneficial_owner",
    "insider",
    "executive",
    "board_member",
    "other",
]

# Domain separator — prefix the canonical bytes so that the hash space
# can never accidentally collide with another payload-hash domain in
# the codebase (filing payload hash, secret-derivation, etc.).
_PAYLOAD_HASH_DOMAIN: Final[bytes] = b"aslan-core/observation/v1\x00"


def _ensure_tz_aware(d: datetime) -> datetime:
    if d.tzinfo is None:
        raise ValueError("datetime must be tz-aware (UTC)")
    return d


def _validate_metadata_finite(meta: Any) -> None:
    """Walk metadata, raise ObservationValidationError on any NaN/Inf
    float. JSON cannot represent these — allowing them creates a
    hash-vs-storage divergence (codex F8, round 4)."""
    if isinstance(meta, bool):
        # bool is a subclass of int — guard before the float branch so
        # True / False don't get treated as floats.
        return
    if isinstance(meta, float):
        if not math.isfinite(meta):
            raise ObservationValidationError(f"metadata float must be finite; got {meta!r}")
        return
    if isinstance(meta, dict):
        for v in meta.values():
            _validate_metadata_finite(v)
        return
    if isinstance(meta, (list, tuple)):
        for v in meta:
            _validate_metadata_finite(v)
        return
    # str / int / None / UUID etc. all pass through unchanged.


def _canonical_json_bytes(meta: dict[str, Any]) -> bytes:
    """RFC 8785 (JCS)-style canonicalisation:
    - sorted keys recursively
    - NFC-normalised UTF-8 strings (keys and values)
    - no whitespace
    - finite floats only (already validated upstream)
    """

    def _norm(v: Any) -> Any:
        if isinstance(v, str):
            return unicodedata.normalize("NFC", v)
        if isinstance(v, dict):
            return {
                unicodedata.normalize("NFC", str(k)): _norm(val)
                for k, val in sorted(v.items(), key=lambda kv: str(kv[0]))
            }
        if isinstance(v, (list, tuple)):
            return [_norm(x) for x in v]
        return v

    return json.dumps(
        _norm(meta),
        separators=(",", ":"),
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


class SubjectRef(BaseModel):
    """A natural person referenced by a series. Persisted into
    ``ts.series_subject``. Used by
    ``ObservationWriter.upsert_series(subjects=[...])``.

    ``role`` MUST match the role CHECK in migration 0013.
    """

    model_config = ConfigDict(frozen=True)

    subject_id: str = Field(min_length=1)
    role: SubjectRole


class Series(BaseModel):
    """One row in ``ts.series_catalog``."""

    model_config = ConfigDict(frozen=True)

    series_id: int | None = None  # populated post-INSERT
    series_code: str = Field(min_length=1)
    source_id: str
    entity_id: UUID | None = None
    metric: str
    frequency: Frequency
    unit: str
    currency_code: str | None = None
    restatement_basis: RestatementBasis = "nominal"
    accounting_standard: str | None = None
    consolidation: str | None = None
    period_type: str | None = None
    description: str | None = None
    pii_class: PiiClass = "none"
    metadata: dict[str, Any] = Field(default_factory=dict)
    subjects: tuple[SubjectRef, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ObservationIn(BaseModel):
    """Input row for ``ObservationWriter.write``. Tz-aware datetimes
    enforced. Exactly one of ``value`` / ``value_text`` required.

    Codex F10 — Pydantic float validators are NOT customised. The
    bare ``float | None`` type means in-memory bits pass through
    unchanged (sign-of-zero, NaN bit pattern, subnormals). The
    ``payload_hash`` packs ``value`` as 8 BE bytes of IEEE-754
    binary64; NEVER as a JSON number.
    """

    model_config = ConfigDict(frozen=True)

    ts: datetime
    as_of: datetime
    value: float | None = None
    value_text: str | None = None
    quality_flag: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ts", "as_of")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _ensure_tz_aware(v)

    @model_validator(mode="after")
    def _exactly_one_value(self) -> ObservationIn:
        has_v = self.value is not None
        has_t = self.value_text is not None
        if not has_v and not has_t:
            raise ValueError("must provide value or value_text")
        if has_v and has_t:
            raise ValueError("must provide exactly one of value, value_text")
        return self

    def payload_hash(self) -> str:
        """SHA256 hex of the canonical (value, value_text, quality_flag,
        metadata) tuple. Split contract:

        - value: 8 BE bytes of IEEE-754 float64 bits (sign-of-zero +
          NaN bit pattern preserved)
        - value_text: NFC-normalised UTF-8, length-prefixed
        - quality_flag: 4 BE bytes (signed int32)
        - metadata: JCS-canonical JSON; finite-floats-only validated
          up front
        """
        _validate_metadata_finite(self.metadata)
        h = hashlib.sha256()
        h.update(_PAYLOAD_HASH_DOMAIN)
        if self.value is not None:
            h.update(b"V")
            # struct '>d' = big-endian binary64; bit-preserving for
            # sign-of-zero, NaN, subnormals (Python's float maps to C
            # double).
            h.update(struct.pack(">d", self.value))
        else:
            h.update(b"v")
        if self.value_text is not None:
            h.update(b"T")
            t = unicodedata.normalize("NFC", self.value_text).encode("utf-8")
            h.update(len(t).to_bytes(4, "big"))
            h.update(t)
        else:
            h.update(b"t")
        h.update(b"Q")
        h.update(self.quality_flag.to_bytes(4, "big", signed=True))
        h.update(b"M")
        meta_bytes = _canonical_json_bytes(self.metadata)
        h.update(len(meta_bytes).to_bytes(4, "big"))
        h.update(meta_bytes)
        return h.hexdigest()


class Observation(BaseModel):
    """Read-back row from ``ts.observation``."""

    model_config = ConfigDict(frozen=True)

    series_id: int
    ts: datetime
    as_of: datetime
    value: float | None
    value_text: str | None
    quality_flag: int
    ingestion_run_id: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class WriteCount(BaseModel):
    """Per-batch counters returned by ``ObservationWriter.write``."""

    model_config = ConfigDict(frozen=True)

    attempted: int
    inserted: int
    updated: int  # always 0 in v0.4 — same-key different-payload raises
    unchanged: int


class SeriesUpsertResult(BaseModel):
    """Return value of ``ObservationWriter.upsert_series``."""

    model_config = ConfigDict(frozen=True)

    series_id: int
    created: bool


__all__ = [
    "Frequency",
    "Observation",
    "ObservationIn",
    "PiiClass",
    "RestatementBasis",
    "Series",
    "SeriesUpsertResult",
    "SubjectRef",
    "SubjectRole",
    "WriteCount",
]
