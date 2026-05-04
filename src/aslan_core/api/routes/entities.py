"""Entity routes: listing, financials, quality scores, prices, NAV."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.api.deps import get_current_user, get_session
from aslan_core.query.entity import get_entity_quality, list_entities
from aslan_core.query.financials import get_entity_financials
from aslan_core.query.prices import get_entity_fundamentals, get_fund_nav, get_stock_prices
from aslan_core.query.schemas import (
    Consolidation,
    EntitySummary,
    PeriodData,
    PeriodType,
    Principal,
    QualityScorePublic,
    RestatementBasis,
    StatementType,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response wrappers
# ---------------------------------------------------------------------------


class EntityListResponse(BaseModel):
    items: list[EntitySummary]
    total: int


class PricePointResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    ts: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None


class NavPointResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    ts: date
    price: Decimal


class FundamentalSnapshotResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    metric: str
    value: Decimal
    as_of: date


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_uuid(raw: str) -> UUID:
    """Parse a UUID string, raising 400 on failure."""
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid UUID: {raw}") from None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=EntityListResponse)
async def list_entities_endpoint(
    search: str | None = Query(default=None, max_length=100),
    has_financials: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    current_user: Principal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> EntityListResponse:
    """Paginated entity listing with optional name search."""
    try:
        items, total = await list_entities(
            session,
            current_user,
            search=search,
            has_financials=has_financials,
            limit=limit,
            offset=offset,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return EntityListResponse(items=items, total=total)


@router.get("/{entity_id}/financials", response_model=list[PeriodData])
async def entity_financials_endpoint(
    entity_id: str,
    statement_type: StatementType | None = Query(default=None),
    period_type: PeriodType = Query(default=PeriodType.QUARTERLY),
    restatement_basis: RestatementBasis = Query(default=RestatementBasis.AS_REPORTED),
    consolidation: Consolidation = Query(default=Consolidation.CONSOLIDATED),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: Principal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[PeriodData]:
    """Canonical financial rows for an entity, grouped by period."""
    uid = _parse_uuid(entity_id)
    try:
        return await get_entity_financials(
            session,
            current_user,
            uid,
            statement_type=statement_type,
            period_type=period_type,
            restatement_basis=restatement_basis,
            consolidation=consolidation,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/{entity_id}/quality", response_model=list[QualityScorePublic])
async def entity_quality_endpoint(
    entity_id: str,
    restatement_basis: RestatementBasis = Query(default=RestatementBasis.AS_REPORTED),
    limit: int = Query(default=10, ge=1, le=100),
    current_user: Principal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[QualityScorePublic]:
    """Quality scores for an entity, ordered by period_end descending."""
    uid = _parse_uuid(entity_id)
    try:
        return await get_entity_quality(
            session,
            current_user,
            uid,
            restatement_basis=restatement_basis,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/{entity_id}/prices", response_model=list[PricePointResponse])
async def entity_prices_endpoint(
    entity_id: str,
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    limit: int = Query(default=250, ge=1, le=2000),
    current_user: Principal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[PricePointResponse]:
    """Daily OHLCV price history for a BIST-listed entity."""
    uid = _parse_uuid(entity_id)
    points = await get_stock_prices(session, uid, start=start, end=end, limit=limit)
    return [
        PricePointResponse(
            ts=p.ts, open=p.open, high=p.high, low=p.low, close=p.close, volume=p.volume
        )
        for p in points
    ]


@router.get("/{entity_id}/nav", response_model=list[NavPointResponse])
async def entity_nav_endpoint(
    entity_id: str,
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    limit: int = Query(default=250, ge=1, le=2000),
    current_user: Principal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[NavPointResponse]:
    """Daily NAV history for a TEFAS fund entity."""
    uid = _parse_uuid(entity_id)
    points = await get_fund_nav(session, uid, start=start, end=end, limit=limit)
    return [NavPointResponse(ts=p.ts, price=p.price) for p in points]


@router.get(
    "/{entity_id}/fundamentals-snapshot",
    response_model=list[FundamentalSnapshotResponse],
)
async def entity_fundamentals_snapshot_endpoint(
    entity_id: str,
    current_user: Principal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[FundamentalSnapshotResponse]:
    """Latest fundamental metrics snapshot (P/E, P/B, ROE, market cap, etc.)."""
    uid = _parse_uuid(entity_id)
    snapshots = await get_entity_fundamentals(session, uid)
    return [
        FundamentalSnapshotResponse(
            metric=s.metric,
            value=s.value,
            as_of=s.as_of,
        )
        for s in snapshots
    ]


__all__ = ["router"]
