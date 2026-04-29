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
    ObjectStoreError,
    ObservationConstraintViolation,
    ObservationError,
    RegistryConstraintViolation,
    RegistryError,
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
