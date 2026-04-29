from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Filing(BaseModel):
    model_config = ConfigDict(frozen=True)

    filing_id: UUID
    source_id: str
    source_filing_ref: str
    entity_id: UUID | None
    kind: str
    subkind: str | None
    title: str
    language: str = Field(min_length=2, max_length=2)
    published_at: datetime
    period_start: date | None
    period_end: date | None
    source_url: str | None
    is_amendment: bool
    previous_filing_id: UUID | None
    primary_object_key: str
    primary_mime: str
    primary_sha256: str = Field(min_length=64, max_length=64)
    primary_bytes: int
    has_xbrl: bool
    xbrl_object_key: str | None
    metadata: dict[str, Any]
    discovered_at: datetime
    revision_no: int

    @field_validator("published_at", "discovered_at")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return v


class FilingAttachment(BaseModel):
    model_config = ConfigDict(frozen=True)

    attachment_id: UUID
    filing_id: UUID
    object_key: str
    mime: str
    sha256: str = Field(min_length=64, max_length=64)
    bytes: int
    role: str
    sequence: int
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return v


class AttachmentIn(BaseModel):
    """Input shape for DocumentStore.put_filing's attachments list.
    The store computes sha256 + bytes; caller provides bytes + mime + filename.
    """

    bytes: bytes
    mime: str
    filename: str
    role: str
    sequence: int = 0


class PutFilingResult(BaseModel):
    """Returned by DocumentStore.put_filing.

    Carries a manifest of every bucket key uploaded by the call:
    primary + xbrl + every attachment. The caller passes this to
    release() on outer-transaction rollback. release() reads from
    this manifest, NOT from doc.filing_attachment (which may be
    invisible/gone post-rollback). This is the codex 2026-04-28 fix.
    """

    filing: Filing
    created: bool
    is_revision: bool
    revision_no: int
    bucket: str
    object_keys: list[str]
