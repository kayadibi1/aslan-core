from aslan_core.schemas.common import (
    EntityStatus,
    EntityType,
    Frequency,
    Namespace,
    RestatementBasis,
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
