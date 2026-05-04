"""Financial routes: cross-entity comparison and single-entity timeseries."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.api.deps import get_current_user, get_session
from aslan_core.query.financials import compare_metric, get_metric_timeseries
from aslan_core.query.schemas import (
    Consolidation,
    MetricPoint,
    PeriodType,
    Principal,
    RestatementBasis,
    TimeseriesPoint,
)

router = APIRouter()


def _parse_uuid(raw: str) -> UUID:
    """Parse a UUID string, raising 400 on failure."""
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid UUID: {raw}") from None


def _validate_canonical_code(request: Request, canonical_code: str) -> None:
    """Check that *canonical_code* exists in the loaded manifest."""
    valid_codes: set[str] | None = getattr(request.app.state, "canonical_codes", None)
    if valid_codes is not None and canonical_code not in valid_codes:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown canonical_code: {canonical_code}",
        )


@router.get("/compare", response_model=dict[str, list[MetricPoint]])
async def compare_metric_endpoint(
    request: Request,
    entity_ids: str = Query(description="Comma-separated entity UUIDs (max 10)"),
    canonical_code: str = Query(),
    restatement_basis: RestatementBasis = Query(default=RestatementBasis.AS_REPORTED),
    period_type: PeriodType = Query(default=PeriodType.QUARTERLY),
    consolidation: Consolidation = Query(default=Consolidation.CONSOLIDATED),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: Principal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, list[MetricPoint]]:
    """Compare a single canonical code across up to 10 entities."""
    _validate_canonical_code(request, canonical_code)

    raw_ids = [s.strip() for s in entity_ids.split(",") if s.strip()]
    if len(raw_ids) > 10:
        raise HTTPException(status_code=400, detail="entity_ids must contain at most 10 UUIDs")
    parsed_ids = [_parse_uuid(r) for r in raw_ids]

    try:
        result = await compare_metric(
            session,
            current_user,
            parsed_ids,
            canonical_code,
            restatement_basis=restatement_basis,
            period_type=period_type,
            consolidation=consolidation,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Convert UUID keys to strings for JSON serialisation
    return {str(k): v for k, v in result.items()}


@router.get("/timeseries", response_model=list[TimeseriesPoint])
async def timeseries_endpoint(
    request: Request,
    entity_id: str = Query(),
    canonical_code: str = Query(),
    restatement_basis: RestatementBasis | None = Query(default=None),
    period_type: PeriodType = Query(default=PeriodType.QUARTERLY),
    consolidation: Consolidation = Query(default=Consolidation.CONSOLIDATED),
    limit: int = Query(default=40, ge=1, le=100),
    current_user: Principal = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TimeseriesPoint]:
    """Time-series for a single canonical code on one entity."""
    _validate_canonical_code(request, canonical_code)
    uid = _parse_uuid(entity_id)

    try:
        return await get_metric_timeseries(
            session,
            current_user,
            uid,
            canonical_code,
            restatement_basis=restatement_basis,
            period_type=period_type,
            consolidation=consolidation,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


__all__ = ["router"]
