"""Bitemporal Research API — response envelope helpers.

Per ``docs/specs/bitemporal-research-api/SCOPE.md`` D15.

Every response goes through :func:`build_envelope` so that ``data``,
``metadata.as_of_resolved``, ``metadata.lineage`` (where applicable),
``metadata.feature_flags_active`` (snapshot), ``metadata.request_id``,
and ``metadata.served_by`` (commit SHA) are uniform across endpoints.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Lineage(BaseModel):
    """Per SCOPE.md D15.

    Fields are nullable; populated where the underlying row carries them.
    """

    model_config = ConfigDict(frozen=True)
    source_filing_id: str | None = None
    raw_bytes_sha256: str | None = None
    extraction_code_version: str | None = None
    prompt_version: str | None = None
    model_identity: str | None = None
    extraction_seed: int | None = None


class Pagination(BaseModel):
    model_config = ConfigDict(frozen=True)
    next_cursor: str | None = None
    prev_cursor: str | None = None
    has_more: bool = False
    total_count_estimate: int | None = None


class Warning(BaseModel):  # noqa: A001 — domain term overrides builtin
    model_config = ConfigDict(frozen=True)
    code: str
    message: str
    rows_affected: int = 0


class EnvelopeMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)
    as_of_requested: datetime | None = None
    as_of_resolved: datetime
    as_of_range: tuple[datetime, datetime] | None = None
    lineage: Lineage | None = None
    pagination: Pagination = Field(default_factory=Pagination)
    feature_flags_active: list[str] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)
    request_id: UUID
    served_by: str


def _commit_sha() -> str:
    """Return the commit SHA the running service was built from.

    Reads ``ASLAN_BUILD_SHA`` env if set; falls back to ``"unknown"``.
    """
    return os.environ.get("ASLAN_BUILD_SHA", "unknown")


def now_utc() -> datetime:
    """UTC now, microsecond-precise per SCOPE.md D12."""
    return datetime.now(tz=timezone.utc)


def build_envelope(
    *,
    data: Any,
    request_id: UUID,
    as_of_requested: datetime | None,
    as_of_resolved: datetime,
    as_of_range: tuple[datetime, datetime] | None = None,
    lineage: Lineage | None = None,
    pagination: Pagination | None = None,
    feature_flags_active: list[str] | None = None,
    warnings: list[Warning] | None = None,
) -> dict[str, Any]:
    """Produce the response envelope per SCOPE.md D15."""
    meta = EnvelopeMetadata(
        as_of_requested=as_of_requested,
        as_of_resolved=as_of_resolved,
        as_of_range=as_of_range,
        lineage=lineage,
        pagination=pagination or Pagination(),
        feature_flags_active=feature_flags_active or [],
        warnings=warnings or [],
        request_id=request_id,
        served_by=f"bitemporal-api/{_commit_sha()}",
    )
    return {"data": data, "metadata": meta.model_dump(mode="json")}


__all__ = [
    "EnvelopeMetadata",
    "Lineage",
    "Pagination",
    "Warning",
    "build_envelope",
    "now_utc",
]
