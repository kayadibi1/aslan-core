from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError


class AslanCoreError(Exception):
    """Base for every error raised inside the package."""


class AslanCoreValidationError(AslanCoreError):
    """Wraps Pydantic ValidationError at the public-API boundary."""

    @classmethod
    def from_pydantic(cls, ve: ValidationError) -> AslanCoreValidationError:
        e = cls(str(ve))
        e.__cause__ = ve
        return e


class ConfigError(AslanCoreError):
    """Malformed Settings or missing required env."""


class DBError(AslanCoreError):
    """Base for DB-side failures."""


class UnknownSource(DBError):
    """ingestion_run() called with a source_id absent from src.source."""

    def __init__(self, source_id: str) -> None:
        super().__init__(f"unknown source_id={source_id!r}; run `aslan seed sources` first")
        self.source_id = source_id


class ConcurrencyError(DBError):
    """CAS / serialization failure."""


class ConstraintViolation(DBError):
    """A DB constraint failed in a way the writer did not anticipate."""


class RegistryError(AslanCoreError):
    pass


class EntityNotFound(RegistryError):
    pass


class IdentifierConflict(RegistryError):
    """add_identifier saw the same (namespace, value, valid_from) for a different entity_id."""


class EntityMergeRequired(RegistryError):
    """create_entity matched identifiers across multiple entities, OR a
    cross-source identifier match was detected. Caller must merge explicitly;
    auto-merge is intentionally same-source-only."""


class RegistryConstraintViolation(RegistryError):
    pass


class ObservationError(AslanCoreError):
    pass


class SeriesNotFound(ObservationError):
    pass


class ObservationConstraintViolation(ObservationError):
    pass


class ObservationValidationError(ObservationError):
    """Raised when an ObservationIn fails canonicalisation (e.g., NaN
    or Inf in metadata, both ``value`` and ``value_text`` set, naive
    datetime). Distinct from a Pydantic ValidationError so callers can
    catch validation failures discovered during hashing without also
    catching shape errors."""


class SeriesCodeConflict(ObservationError):
    """Raised by ObservationWriter.upsert_series when an existing
    series_code is being upserted with metadata fields that the spec
    forbids changing (e.g., changing source_id or frequency on a
    series that already has observations)."""


class ObservationConflict(ObservationError):
    """Codex F2 — raised by ObservationWriter.write when a batch
    contains an observation whose ``(series_id, ts, as_of)`` matches
    an existing row (or another row in the same batch) but the payload
    hash differs.

    Restatements must use a fresh ``as_of``; this error tells the
    caller their batch is doing something else (a retry with different
    bytes? a clock-skew bug? the producer flipped the value between
    attempts?). The whole batch is rolled back; partial writes are
    forbidden because they corrupt forensic history.
    """

    def __init__(
        self,
        message: str,
        *,
        key: tuple[int, datetime, datetime] | None = None,
        existing_hash: str | None = None,
        new_hash: str | None = None,
    ) -> None:
        super().__init__(message)
        self.key = key
        self.existing_hash = existing_hash
        self.new_hash = new_hash


class IdentifyingSeriesPiiInClearText(ObservationError):
    """Codex F4 — raised by ObservationWriter.upsert_series when
    ``pii_class='identifying'`` and ``series_code`` or ``description``
    matches a PII-shape pattern (email regex, common name patterns).

    Forces PII into the structured ``ts.series_subject`` +
    ``metadata.subjects`` path so Art. 17 deletion can find it.
    """


class IdentifyingSeriesMissingSubject(ObservationError):
    """Codex F18, 2026-04-29 — raised by
    ObservationWriter.upsert_series when ``pii_class='identifying'``
    is set but the upsert provides zero SubjectRefs (and no existing
    ``ts.series_subject`` rows survive).

    An identifying series with no subject handle is structurally
    undeletable under Art. 17 because the deletion runtime indexes by
    ``subject_id -> series_id``.
    """


class IdentifyingSeriesMetadataPii(ObservationError):
    """Codex F18, 2026-04-29 — raised by
    ObservationWriter.upsert_series when ``pii_class='identifying'``
    and the free-form ``metadata`` JSONB contains a value (outside the
    structured ``subjects`` / ``fields`` shape) matching a PII-shape
    regex. Forces PII to the structured channels.
    """


class MetadataSchemaViolation(ObservationError):
    """Codex F24, 2026-04-29 — raised by
    ObservationWriter.upsert_series when ``metadata`` contains an
    object key that violates the metadata-schema contract.

    v0.4 currently rejects any string key (at any nesting depth) whose
    value matches ``^\\d+$`` (positive integer / non-negative integer
    in decimal, including leading-zero forms like ``"01"``). Such keys
    are indistinguishable from array indices once flattened by
    ``path_to_jsonb_set_text_array`` for ``jsonb_set`` — both
    ``{"fields": {"0": "..."}}`` (object key) and
    ``{"fields": ["..."]}`` (array index 0) yield ``['fields', '0']``
    and the Art. 17 scrub UPDATE could land on the wrong leaf because
    Postgres ``jsonb_set`` dispatches by container kind at evaluation
    time, not by Python type. Use a non-numeric prefix
    (``"item_0"`` / ``"row_0"`` / ``"_0"``) instead.

    Applies to ALL series regardless of ``pii_class`` so the metadata
    shape is uniform across the deletion runtime's surface.
    """


class DocumentError(AslanCoreError):
    pass


class ObjectStoreError(DocumentError):
    pass


class DocumentDBError(DocumentError):
    """Object uploaded but the DB write failed."""


class FilingNotFound(DocumentError):
    pass


class DocumentNotFound(DocumentError):
    """Raised by DocumentStore.get_filing / amendment_chain when the
    filing_id is unknown."""


class WatermarkError(AslanCoreError):
    pass


class WatermarkRegression(WatermarkError):
    """advance() saw an unexpected current cursor."""


class StreamError(AslanCoreError):
    pass


class StreamPublishError(StreamError):
    pass


class StreamReadError(StreamError):
    pass


class StreamDeserializeError(StreamError):
    """Payload didn't match the registered Pydantic event model."""


class AuditError(AslanCoreError):
    """Base for audit-subsystem failures."""


class AuditMissingActor(AuditError):
    """Raised when a mutation runs without an actor set in the
    ContextVar AND ``Settings.audit_strict`` is True. In non-strict mode
    a warning is logged and the mutation proceeds with
    ``actor_id='system:unknown'``."""
