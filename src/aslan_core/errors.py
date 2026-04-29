from __future__ import annotations

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
