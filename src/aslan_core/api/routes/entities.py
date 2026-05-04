"""Entity routes: listing, financials, quality scores."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.api.deps import get_current_user, get_session
from aslan_core.query.entity import get_entity_quality, list_entities
from aslan_core.query.financials import get_entity_financials
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


__all__ = ["router"]
