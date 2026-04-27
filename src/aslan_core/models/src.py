from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aslan_core.models import Base


class Source(Base):
    __tablename__ = "source"
    __table_args__ = ({"schema": "src"},)

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text)
    license_status: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )


class IngestionRun(Base):
    __tablename__ = "ingestion_run"
    __table_args__ = (
        Index("run_source_started", "source_id", "started_at"),
        {"schema": "src"},
    )

    ingestion_run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("src.source.source_id"),
        nullable=False,
    )
    job_name: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column()
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="running")
    error_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    rows_written: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    docs_written: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    bytes_written: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    config_hash: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )


class Watermark(Base):
    __tablename__ = "watermark"
    __table_args__ = ({"schema": "src"},)

    source_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("src.source.source_id"),
        primary_key=True,
    )
    job_name: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    cursor_value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
