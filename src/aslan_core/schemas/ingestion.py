from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class IngestionRunHandle(BaseModel):
    """Pydantic mirror of the ingestion-run handle.

    The runtime ``IngestionRunHandle`` class (in
    ``aslan_core.ingestion.run``) is the behavioral object; this is the
    wire/serialization shape.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    source_id: str
    job_name: str
    started_at: datetime


class RunStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    source_id: str
    job_name: str
    started_at: datetime
    finished_at: datetime | None
    status: str  # 'running', 'succeeded', 'failed'
    rows_written: int
    docs_written: int
    bytes_written: int
    error_count: int
    error: str | None


class WatermarkValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    job_name: str
    key: str
    cursor_value: str
    updated_at: datetime
