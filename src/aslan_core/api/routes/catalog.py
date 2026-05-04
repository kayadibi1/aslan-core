"""Catalog route: list canonical financial line items (no auth)."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from aslan_core.query.catalog import list_canonical_lines
from aslan_core.query.schemas import CanonicalLineInfo, StatementType

router = APIRouter()


@router.get("/canonical-lines", response_model=list[CanonicalLineInfo])
async def canonical_lines_endpoint(
    request: Request,
    statement_type: StatementType | None = Query(default=None),
) -> list[CanonicalLineInfo]:
    """List canonical financial line item codes. No authentication required."""
    all_lines: list[CanonicalLineInfo] = getattr(request.app.state, "canonical_lines", [])
    return list_canonical_lines(all_lines, statement_type=statement_type)


__all__ = ["router"]
