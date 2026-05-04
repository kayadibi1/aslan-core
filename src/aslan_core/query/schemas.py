"""Query-layer schemas and enums for the financials API.

Provides:
- Enums:  StatementType, PeriodType, RestatementBasis, Consolidation
- Auth:   Principal dataclass + AI_PRINCIPAL sentinel
- Response models: EntitySummary, PeriodData, MetricPoint,
                   TimeseriesPoint, QualityCheckPublic, QualityScorePublic,
                   CanonicalLineInfo
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class StatementType(StrEnum):
    """Financial statement type."""

    IS = "is"
    BS = "bs"
    CF = "cf"


class PeriodType(StrEnum):
    """Reporting period granularity."""

    QUARTERLY = "q"
    HALF_YEAR = "h"
    ANNUAL = "y"
    YTD = "ytd"


class RestatementBasis(StrEnum):
    """Restatement / normalisation basis for a financial value."""

    AS_REPORTED = "as_reported"
    CPI_NORMALIZED = "cpi_normalized"


class Consolidation(StrEnum):
    """Whether the filing covers a consolidated or unconsolidated entity."""

    CONSOLIDATED = "consolidated"
    UNCONSOLIDATED = "unconsolidated"


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Caller identity propagated through the query layer."""

    user_id: UUID
    role: str
    is_active: bool


AI_PRINCIPAL = Principal(
    user_id=UUID("00000000-0000-0000-0000-000000000000"),
    role="service",
    is_active=True,
)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class EntitySummary(BaseModel):
    """High-level summary row returned by the entity listing endpoint."""

    model_config = ConfigDict(frozen=True)

    entity_id: UUID
    legal_name: str
    ticker: str | None
    latest_period: date | None
    avg_quality_score: int | None
    canonical_line_count: int


class PeriodData(BaseModel):
    """One complete period snapshot for an entity and statement type."""

    model_config = ConfigDict(frozen=True)

    period_end: date
    period_type: PeriodType
    consolidation: Consolidation
    restatement_basis: RestatementBasis
    lines: dict[str, Decimal | None]
    quality_score: int | None


class MetricPoint(BaseModel):
    """Single metric value at a given period."""

    model_config = ConfigDict(frozen=True)

    period_end: date
    period_type: PeriodType
    value: Decimal | None
    restatement_basis: RestatementBasis


class TimeseriesPoint(BaseModel):
    """Both restatement variants for a single metric at a single period."""

    model_config = ConfigDict(frozen=True)

    period_end: date
    period_type: PeriodType
    as_reported: Decimal | None
    cpi_normalized: Decimal | None
    cpi_base_date: date | None


class QualityCheckPublic(BaseModel):
    """Result of a single data-quality check, safe to expose externally."""

    model_config = ConfigDict(frozen=True)

    state: str
    delta_pct: float | None = None
    coverage_pct: float | None = None


class QualityScorePublic(BaseModel):
    """Composite quality score for a single period, with per-check detail."""

    model_config = ConfigDict(frozen=True)

    period_end: date
    period_type: PeriodType
    consolidation: Consolidation
    score: int = Field(ge=0, le=100)
    insufficient_data: bool
    checks: dict[str, QualityCheckPublic]


class CanonicalLineInfo(BaseModel):
    """Metadata for a canonical financial line item code."""

    model_config = ConfigDict(frozen=True)

    canonical_code: str
    statement_type: StatementType
    description: str
    computed: bool
    required: bool
    monetary: bool


__all__ = [
    "AI_PRINCIPAL",
    "CanonicalLineInfo",
    "Consolidation",
    "EntitySummary",
    "MetricPoint",
    "PeriodData",
    "PeriodType",
    "Principal",
    "QualityCheckPublic",
    "QualityScorePublic",
    "RestatementBasis",
    "StatementType",
    "TimeseriesPoint",
]
