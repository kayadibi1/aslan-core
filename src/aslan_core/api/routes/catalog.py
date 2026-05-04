"""Catalog route: canonical lines + generic series query (no auth)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from aslan_core.api.deps import get_session
from aslan_core.query.catalog import list_canonical_lines
from aslan_core.query.prices import get_observation_series
from aslan_core.query.schemas import CanonicalLineInfo, StatementType

router = APIRouter()


class SeriesPointResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    ts: date
    value: Decimal


@router.get("/canonical-lines", response_model=list[CanonicalLineInfo])
async def canonical_lines_endpoint(
    request: Request,
    statement_type: StatementType | None = Query(default=None),
) -> list[CanonicalLineInfo]:
    """List canonical financial line item codes. No authentication required."""
    all_lines: list[CanonicalLineInfo] = getattr(request.app.state, "canonical_lines", [])
    return list_canonical_lines(all_lines, statement_type=statement_type)


@router.get("/series/{series_code:path}", response_model=list[SeriesPointResponse])
async def series_endpoint(
    series_code: str,
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> list[SeriesPointResponse]:
    """Query any observation series by code. No authentication required."""
    points = await get_observation_series(session, series_code, start=start, end=end, limit=limit)
    return [SeriesPointResponse(ts=d, value=v) for d, v in points]


__all__ = ["router"]
