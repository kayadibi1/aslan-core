"""Private SQLAlchemy ORM models for the ``ts.*`` schema.

NOT public API. Consumers must use
``aslan_core.timeseries.ObservationWriter`` /
``ObservationReader`` and the Pydantic models in
``aslan_core.schemas.timeseries``.

The audit.observation_batch_keys hypertable is intentionally NOT
modelled here — it's only ever written via raw ``text()`` inside
``ObservationWriter.write`` (Phase-2 bulk-INSERT path), and queried
forensically via raw SQL. Adding an ORM class would tempt callers
into row-by-row writes which the spec forbids.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from aslan_core.models import Base


class SeriesCatalog(Base):
    __tablename__ = "series_catalog"
    __table_args__ = (
        Index(
            "series_catalog_entity",
            "entity_id",
            postgresql_where=text("entity_id IS NOT NULL"),
        ),
        Index("series_catalog_source_metric", "source_id", "metric"),
        Index(
            "series_catalog_pii",
            "pii_class",
            postgresql_where=text("pii_class != 'none'"),
        ),
        {"schema": "ts"},
    )

    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    series_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    frequency: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    currency_code: Mapped[str | None] = mapped_column(String(3))
    restatement_basis: Mapped[str] = mapped_column(Text, nullable=False, default="nominal")
    accounting_standard: Mapped[str | None] = mapped_column(Text)
    consolidation: Mapped[str | None] = mapped_column(Text)
    period_type: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    pii_class: Mapped[str] = mapped_column(Text, nullable=False, default="none")
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    actor_id: Mapped[str | None] = mapped_column(Text)
    actor_kind: Mapped[str | None] = mapped_column(Text)
    client_ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Observation(Base):
    __tablename__ = "observation"
    __table_args__ = (
        CheckConstraint(
            "(value IS NULL) <> (value_text IS NULL)",
            name="observation_value_check",
        ),
        Index("observation_series_ts_as_of", "series_id", "ts", "as_of"),
        Index("observation_run", "ingestion_run_id"),
        {"schema": "ts"},
    )

    series_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ts.series_catalog.series_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    # Float() maps to PostgreSQL DOUBLE PRECISION (codex F10 — no NUMERIC).
    value: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str | None] = mapped_column(Text)
    quality_flag: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    ingestion_run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    actor_id: Mapped[str | None] = mapped_column(Text)
    actor_kind: Mapped[str | None] = mapped_column(Text)
    client_ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))


class SeriesSubject(Base):
    __tablename__ = "series_subject"
    __table_args__ = (
        Index("series_subject_lookup", "subject_id", "role"),
        {"schema": "ts"},
    )

    series_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ts.series_catalog.series_id", ondelete="CASCADE"),
        primary_key=True,
    )
    subject_id: Mapped[str] = mapped_column(Text, primary_key=True)
    role: Mapped[str] = mapped_column(Text, primary_key=True)
    actor_id: Mapped[str | None] = mapped_column(Text)
    actor_kind: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
