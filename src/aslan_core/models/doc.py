from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BIGINT,
    CHAR,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from aslan_core.models import Base


class Filing(Base):
    __tablename__ = "filing"
    __table_args__ = (
        UniqueConstraint("source_id", "primary_sha256", name="filing_hash_uniq"),
        UniqueConstraint(
            "source_id",
            "source_filing_ref",
            "revision_no",
            name="filing_revision_uniq",
        ),
        Index("filing_entity_pub", "entity_id", "published_at"),
        Index("filing_kind_pub", "kind", "published_at"),
        Index("filing_source_ref_latest", "source_id", "source_filing_ref", "revision_no"),
        Index(
            "filing_amendment_chain",
            "previous_filing_id",
            postgresql_where=sa_text("previous_filing_id IS NOT NULL"),
        ),
        {"schema": "doc"},
    )

    filing_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=sa_text("gen_random_uuid()"),
    )
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("src.source.source_id"), nullable=False)
    source_filing_ref: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ref.entity.entity_id", ondelete="RESTRICT"),
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    subkind: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(CHAR(2), nullable=False, server_default="tr")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_start: Mapped[date | None] = mapped_column()
    period_end: Mapped[date | None] = mapped_column()
    source_url: Mapped[str | None] = mapped_column(Text)
    is_amendment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    previous_filing_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("doc.filing.filing_id")
    )
    primary_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    primary_mime: Mapped[str] = mapped_column(Text, nullable=False)
    primary_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    primary_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    extracted_text_key: Mapped[str | None] = mapped_column(Text)
    has_xbrl: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("false"))
    xbrl_object_key: Mapped[str | None] = mapped_column(Text)
    # ORM attribute name "metadata_" because Base.metadata is reserved by SQLAlchemy.
    # Maps to "metadata" column.
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=sa_text("'{}'::jsonb"),
    )
    ingestion_run_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("src.ingestion_run.ingestion_run_id"),
        nullable=False,
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)


class FilingAttachment(Base):
    __tablename__ = "filing_attachment"
    __table_args__ = (
        UniqueConstraint("filing_id", "sha256", name="filing_attachment_hash_uniq"),
        Index("att_filing", "filing_id"),
        {"schema": "doc"},
    )

    attachment_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=sa_text("gen_random_uuid()"),
    )
    filing_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("doc.filing.filing_id", ondelete="CASCADE"),
        nullable=False,
    )
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )


class FilingBody(Base):
    __tablename__ = "filing_body"
    __table_args__ = (
        # body_fts is a GENERATED ALWAYS tsvector column managed by migrations;
        # reference via sa_text so SQLAlchemy doesn't look it up in the mapper.
        Index("filing_body_fts", sa_text("body_fts"), postgresql_using="gin"),
        {"schema": "doc"},
    )

    filing_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("doc.filing.filing_id", ondelete="CASCADE"),
        primary_key=True,
    )
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_lang: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
