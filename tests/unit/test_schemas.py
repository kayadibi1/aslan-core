from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aslan_core.schemas.common import (
    EntityStatus,
    EntityType,
    Frequency,
    Namespace,
    RestatementBasis,
)
from aslan_core.schemas.entity import (
    Entity,
    EntityMatch,
    Identifier,
    IdentifierIn,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _make_entity() -> Entity:
    return Entity(
        entity_id=uuid4(),
        type="company",
        legal_name="X",
        short_name=None,
        country_code="TR",
        domicile=None,
        incorporation_dt=None,
        status="active",
        fiscal_year_end=None,
        parent_entity_id=None,
        metadata={},
        source_id="kap",
        ingestion_run_id=1,
        created_at=_now(),
        updated_at=_now(),
    )


def test_entity_type_values() -> None:
    assert EntityType.COMPANY.value == "company"
    assert EntityType.FUND.value == "fund"
    assert {e.value for e in EntityType} >= {
        "company",
        "fund",
        "instrument",
        "index",
        "sovereign",
        "sector",
        "founder",
        "other",
    }


def test_entity_status_values() -> None:
    assert {s.value for s in EntityStatus} == {
        "active",
        "suspended",
        "delisted",
        "merged",
        "dissolved",
    }


def test_namespace_canonical_set() -> None:
    assert Namespace.BIST_TICKER.value == "bist_ticker"
    assert Namespace.KAP_ENTITY_CODE.value == "kap_entity_code"
    assert Namespace.TAX_NUMBER.value == "tax_number"


def test_frequency_values_present() -> None:
    assert Frequency.DAILY.value == "daily"
    assert Frequency.QUARTERLY.value == "quarterly"


def test_restatement_basis_default_nominal() -> None:
    assert RestatementBasis.NOMINAL.value == "nominal"
    assert RestatementBasis.TAS29_REAL.value == "tas29_real"


def test_entity_is_frozen() -> None:
    e = _make_entity()
    with pytest.raises(ValidationError):
        e.legal_name = "Y"  # type: ignore[misc]


def test_identifier_in_defaults() -> None:
    iin = IdentifierIn(namespace="bist_ticker", value="ASELS")
    assert iin.valid_from is None
    assert iin.valid_to is None
    assert iin.is_primary is False


def test_entity_match_carries_similarity() -> None:
    e = _make_entity()
    em = EntityMatch(entity=e, matched_identifiers=[], similarity=0.42)
    assert em.similarity == 0.42
    assert em.entity.entity_id == e.entity_id


def test_identifier_is_frozen() -> None:
    ident = Identifier(
        identifier_id=1,
        entity_id=uuid4(),
        namespace="bist_ticker",
        value="ASELS",
        valid_from=_now().date(),
        valid_to=_now().date(),
        is_primary=True,
        source_id="kap",
        ingestion_run_id=1,
        created_at=_now(),
    )
    assert ident.namespace == "bist_ticker"
    with pytest.raises(ValidationError):
        ident.value = "OTHER"  # type: ignore[misc]
