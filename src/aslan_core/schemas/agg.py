from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class RestatementConfigSchema(BaseModel):
    config_id: int
    name: str
    cpi_series_code: str
    base_date: date
    applies_from: date
    applies_to: date
    method: str
    description: str | None
    created_at: datetime
