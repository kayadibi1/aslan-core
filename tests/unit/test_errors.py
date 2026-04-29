from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from aslan_core.errors import (
    AslanCoreError,
    AslanCoreValidationError,
    AuditError,
    AuditMissingActor,
    ConcurrencyError,
    ConfigError,
    ConstraintViolation,
    DBError,
    DocumentDBError,
    DocumentError,
    EntityMergeRequired,
    EntityNotFound,
    FilingNotFound,
    IdentifierConflict,
    IdentifyingSeriesMetadataPii,
    IdentifyingSeriesMissingSubject,
    IdentifyingSeriesPiiInClearText,
    MetadataSchemaViolation,
    ObjectStoreError,
    ObservationConflict,
    ObservationConstraintViolation,
    ObservationError,
    ObservationValidationError,
    RegistryConstraintViolation,
    RegistryError,
    SeriesCodeConflict,
    SeriesNotFound,
    StreamDeserializeError,
    StreamError,
    StreamPublishError,
    StreamReadError,
    UnknownSource,
    WatermarkError,
    WatermarkRegression,
)


@pytest.mark.parametrize(
    "cls",
    [
        ConfigError,
        DBError,
        UnknownSource,
        ConcurrencyError,
        ConstraintViolation,
        RegistryError,
        EntityNotFound,
        IdentifierConflict,
        EntityMergeRequired,
        RegistryConstraintViolation,
        ObservationError,
        SeriesNotFound,
        ObservationConstraintViolation,
        DocumentError,
        ObjectStoreError,
        DocumentDBError,
        FilingNotFound,
        WatermarkError,
        WatermarkRegression,
        StreamError,
        StreamPublishError,
        StreamReadError,
        StreamDeserializeError,
        AuditError,
        AuditMissingActor,
    ],
)
def test_all_errors_inherit_aslan_core_error(cls: type[Exception]) -> None:
    assert issubclass(cls, AslanCoreError)


def test_unknown_source_carries_id() -> None:
    err = UnknownSource("kap")
    assert err.source_id == "kap"
    assert "kap" in str(err)


def test_validation_wrapping_chains_original() -> None:
    class M(BaseModel):
        x: int

    try:
        M(x="not-an-int")
    except ValidationError as ve:
        wrapped = AslanCoreValidationError.from_pydantic(ve)
        assert wrapped.__cause__ is ve


# v0.4.0 timeseries error types ---------------------------------------------


@pytest.mark.parametrize(
    "cls",
    [
        SeriesCodeConflict,
        ObservationValidationError,
        ObservationConflict,
        IdentifyingSeriesPiiInClearText,
        IdentifyingSeriesMissingSubject,
        IdentifyingSeriesMetadataPii,
        MetadataSchemaViolation,
    ],
)
def test_v0_4_timeseries_errors_inherit_aslan_core_error(
    cls: type[Exception],
) -> None:
    assert issubclass(cls, AslanCoreError)


def test_observation_conflict_carries_diagnostic_fields() -> None:
    k = (
        1,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    )
    e = ObservationConflict("boom", key=k, existing_hash="a", new_hash="b")
    assert e.key == k
    assert e.existing_hash == "a"
    assert e.new_hash == "b"


def test_observation_conflict_minimal_construction() -> None:
    e = ObservationConflict("boom")
    assert e.key is None
    assert e.existing_hash is None
    assert e.new_hash is None
