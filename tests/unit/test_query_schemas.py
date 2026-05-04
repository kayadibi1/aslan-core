"""Unit tests for ``aslan_core.query.schemas``.

Covers: enum values, Principal immutability, model serialization,
Decimal (not float) for monetary fields, and QualityScorePublic score
range constraint.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from aslan_core.query.schemas import (
    AI_PRINCIPAL,
    CanonicalLineInfo,
    Consolidation,
    EntitySummary,
    MetricPoint,
    PeriodData,
    PeriodType,
    Principal,
    QualityCheckPublic,
    QualityScorePublic,
    RestatementBasis,
    StatementType,
    TimeseriesPoint,
)

# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------


def test_statement_type_values() -> None:
    assert str(StatementType.IS) == "is"
    assert str(StatementType.BS) == "bs"
    assert str(StatementType.CF) == "cf"


def test_period_type_values() -> None:
    assert str(PeriodType.QUARTERLY) == "q"
    assert str(PeriodType.HALF_YEAR) == "h"
    assert str(PeriodType.ANNUAL) == "y"
    assert str(PeriodType.YTD) == "ytd"


def test_restatement_basis_values() -> None:
    assert str(RestatementBasis.AS_REPORTED) == "as_reported"
    assert str(RestatementBasis.CPI_NORMALIZED) == "cpi_normalized"


def test_consolidation_values() -> None:
    assert str(Consolidation.CONSOLIDATED) == "consolidated"
    assert str(Consolidation.UNCONSOLIDATED) == "unconsolidated"


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------


def test_principal_is_frozen() -> None:
    p = Principal(
        user_id=UUID("12345678-0000-0000-0000-000000000000"),
        role="analyst",
        is_active=True,
    )
    with pytest.raises((AttributeError, TypeError)):
        p.role = "admin"  # type: ignore[misc]


def test_ai_principal_sentinel() -> None:
    assert AI_PRINCIPAL.user_id == UUID("00000000-0000-0000-0000-000000000000")
    assert AI_PRINCIPAL.role == "service"
    assert AI_PRINCIPAL.is_active is True


def test_ai_principal_is_frozen() -> None:
    with pytest.raises((AttributeError, TypeError)):
        AI_PRINCIPAL.is_active = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# EntitySummary
# ---------------------------------------------------------------------------


def test_entity_summary_round_trip() -> None:
    es = EntitySummary(
        entity_id=UUID("aaaaaaaa-0000-0000-0000-000000000000"),
        legal_name="Acme Corp",
        ticker="ACM",
        latest_period=date(2025, 12, 31),
        avg_quality_score=87,
        canonical_line_count=42,
    )
    assert es.legal_name == "Acme Corp"
    assert es.ticker == "ACM"
    assert es.canonical_line_count == 42


def test_entity_summary_nullable_fields() -> None:
    es = EntitySummary(
        entity_id=UUID("aaaaaaaa-0000-0000-0000-000000000000"),
        legal_name="Acme Corp",
        ticker=None,
        latest_period=None,
        avg_quality_score=None,
        canonical_line_count=0,
    )
    assert es.ticker is None
    assert es.latest_period is None
    assert es.avg_quality_score is None


# ---------------------------------------------------------------------------
# PeriodData — Decimal not float
# ---------------------------------------------------------------------------


def test_period_data_uses_decimal() -> None:
    pd = PeriodData(
        period_end=date(2025, 12, 31),
        period_type=PeriodType.ANNUAL,
        consolidation=Consolidation.CONSOLIDATED,
        restatement_basis=RestatementBasis.AS_REPORTED,
        lines={"revenue": Decimal("1234567.89"), "ebitda": None},
        quality_score=95,
    )
    assert isinstance(pd.lines["revenue"], Decimal)
    assert pd.lines["ebitda"] is None


def test_period_data_frozen() -> None:
    pd = PeriodData(
        period_end=date(2025, 12, 31),
        period_type=PeriodType.QUARTERLY,
        consolidation=Consolidation.CONSOLIDATED,
        restatement_basis=RestatementBasis.AS_REPORTED,
        lines={},
        quality_score=None,
    )
    with pytest.raises((ValidationError, AttributeError, TypeError)):
        pd.quality_score = 50  # type: ignore[misc]


# ---------------------------------------------------------------------------
# MetricPoint — Decimal not float
# ---------------------------------------------------------------------------


def test_metric_point_uses_decimal() -> None:
    mp = MetricPoint(
        period_end=date(2025, 9, 30),
        period_type=PeriodType.QUARTERLY,
        value=Decimal("999.00"),
        restatement_basis=RestatementBasis.CPI_NORMALIZED,
    )
    assert isinstance(mp.value, Decimal)
    assert mp.value == Decimal("999.00")


def test_metric_point_nullable_value() -> None:
    mp = MetricPoint(
        period_end=date(2025, 9, 30),
        period_type=PeriodType.QUARTERLY,
        value=None,
        restatement_basis=RestatementBasis.AS_REPORTED,
    )
    assert mp.value is None


# ---------------------------------------------------------------------------
# TimeseriesPoint — Decimal not float
# ---------------------------------------------------------------------------


def test_timeseries_point_both_bases() -> None:
    tp = TimeseriesPoint(
        period_end=date(2024, 12, 31),
        period_type=PeriodType.ANNUAL,
        as_reported=Decimal("500000"),
        cpi_normalized=Decimal("450000"),
        cpi_base_date=date(2020, 1, 1),
    )
    assert isinstance(tp.as_reported, Decimal)
    assert isinstance(tp.cpi_normalized, Decimal)
    assert tp.cpi_base_date == date(2020, 1, 1)


def test_timeseries_point_nullable_fields() -> None:
    tp = TimeseriesPoint(
        period_end=date(2024, 12, 31),
        period_type=PeriodType.ANNUAL,
        as_reported=None,
        cpi_normalized=None,
        cpi_base_date=None,
    )
    assert tp.as_reported is None
    assert tp.cpi_normalized is None
    assert tp.cpi_base_date is None


# ---------------------------------------------------------------------------
# QualityCheckPublic
# ---------------------------------------------------------------------------


def test_quality_check_public_defaults() -> None:
    qc = QualityCheckPublic(state="pass")
    assert qc.state == "pass"
    assert qc.delta_pct is None
    assert qc.coverage_pct is None


def test_quality_check_public_with_values() -> None:
    qc = QualityCheckPublic(state="warn", delta_pct=2.5, coverage_pct=98.0)
    assert qc.delta_pct == 2.5
    assert qc.coverage_pct == 98.0


# ---------------------------------------------------------------------------
# QualityScorePublic — score constrained 0-100
# ---------------------------------------------------------------------------


def test_quality_score_public_valid() -> None:
    qs = QualityScorePublic(
        period_end=date(2025, 12, 31),
        period_type=PeriodType.ANNUAL,
        consolidation=Consolidation.CONSOLIDATED,
        score=85,
        insufficient_data=False,
        checks={"balance_check": QualityCheckPublic(state="pass")},
    )
    assert qs.score == 85
    assert qs.insufficient_data is False
    assert "balance_check" in qs.checks


def test_quality_score_public_boundary_values() -> None:
    for score in (0, 50, 100):
        qs = QualityScorePublic(
            period_end=date(2025, 12, 31),
            period_type=PeriodType.ANNUAL,
            consolidation=Consolidation.CONSOLIDATED,
            score=score,
            insufficient_data=False,
            checks={},
        )
        assert qs.score == score


def test_quality_score_public_rejects_below_zero() -> None:
    with pytest.raises(ValidationError):
        QualityScorePublic(
            period_end=date(2025, 12, 31),
            period_type=PeriodType.ANNUAL,
            consolidation=Consolidation.CONSOLIDATED,
            score=-1,
            insufficient_data=False,
            checks={},
        )


def test_quality_score_public_rejects_above_100() -> None:
    with pytest.raises(ValidationError):
        QualityScorePublic(
            period_end=date(2025, 12, 31),
            period_type=PeriodType.ANNUAL,
            consolidation=Consolidation.CONSOLIDATED,
            score=101,
            insufficient_data=False,
            checks={},
        )


# ---------------------------------------------------------------------------
# CanonicalLineInfo
# ---------------------------------------------------------------------------


def test_canonical_line_info_round_trip() -> None:
    cli = CanonicalLineInfo(
        canonical_code="REVENUE",
        statement_type=StatementType.IS,
        description="Total revenue",
        computed=False,
        required=True,
        monetary=True,
    )
    assert cli.canonical_code == "REVENUE"
    assert cli.statement_type == StatementType.IS
    assert cli.monetary is True


def test_canonical_line_info_all_statement_types() -> None:
    for st in StatementType:
        cli = CanonicalLineInfo(
            canonical_code="X",
            statement_type=st,
            description="desc",
            computed=False,
            required=False,
            monetary=False,
        )
        assert cli.statement_type == st
