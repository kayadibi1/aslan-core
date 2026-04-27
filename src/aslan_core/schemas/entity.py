from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Entity(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_id: UUID
    type: str
    legal_name: str
    short_name: str | None
    country_code: str
    domicile: str | None
    incorporation_dt: date | None
    status: str
    fiscal_year_end: date | None
    parent_entity_id: UUID | None
    metadata: dict[str, Any]
    source_id: str
    ingestion_run_id: int
    created_at: datetime
    updated_at: datetime


class Identifier(BaseModel):
    model_config = ConfigDict(frozen=True)

    identifier_id: int
    entity_id: UUID
    namespace: str
    value: str
    valid_from: date
    valid_to: date  # FIRST INVALID DAY (half-open)
    is_primary: bool
    source_id: str
    ingestion_run_id: int
    created_at: datetime


class IdentifierIn(BaseModel):
    namespace: str
    value: str
    valid_from: date | None = None
    valid_to: date | None = None
    is_primary: bool = False


class EntityMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity: Entity
    matched_identifiers: list[Identifier]
    similarity: float
