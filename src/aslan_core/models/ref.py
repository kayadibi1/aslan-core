from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from aslan_core.models import Base


class Entity(Base):
    __tablename__ = "entity"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('company','fund','instrument','index','sovereign',"
            "'sector','founder','other')",
            name="entity_type_check",
        ),
        CheckConstraint(
            "status IN ('active','suspended','delisted','merged','dissolved')",
            name="entity_status_check",
        ),
        {"schema": "ref"},
    )

    entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    legal_name: Mapped[str] = mapped_column(Text, nullable=False)
    short_name: Mapped[str | None] = mapped_column(Text)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, server_default="TR")
    domicile: Mapped[str | None] = mapped_column(Text)
    incorporation_dt: Mapped[date | None] = mapped_column(Date)
    fiscal_year_end: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    parent_entity_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ref.entity.entity_id"),
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    ingestion_run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class Identifier(Base):
    __tablename__ = "identifier"
    __table_args__ = (
        Index("identifier_lookup", "namespace", "value"),
        Index("identifier_entity", "entity_id"),
        {"schema": "ref"},
    )

    identifier_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ref.entity.entity_id", ondelete="RESTRICT"),
        nullable=False,
    )
    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        server_default=text("'1900-01-01'"),
    )
    valid_to: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        server_default=text("'9999-12-31'"),
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    ingestion_run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class EntityRelationship(Base):
    __tablename__ = "entity_relationship"
    __table_args__ = (
        CheckConstraint("parent_id <> child_id", name="parent_neq_child"),
        UniqueConstraint(
            "parent_id",
            "child_id",
            "rel_type",
            "valid_from",
            name="uq_relationship_parent_child_type_from",
        ),
        Index("rel_parent", "parent_id", "rel_type"),
        Index("rel_child", "child_id", "rel_type"),
        {"schema": "ref"},
    )

    relationship_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    parent_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ref.entity.entity_id"),
        nullable=False,
    )
    child_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ref.entity.entity_id"),
        nullable=False,
    )
    rel_type: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[Decimal | None] = mapped_column(Numeric)
    valid_from: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        server_default=text("'1900-01-01'"),
    )
    valid_to: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        server_default=text("'9999-12-31'"),
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    ingestion_run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class Sector(Base):
    __tablename__ = "sector"
    __table_args__ = (
        UniqueConstraint("taxonomy", "code", name="uq_sector_taxonomy_code"),
        {"schema": "ref"},
    )

    sector_id: Mapped[str] = mapped_column(Text, primary_key=True)
    taxonomy: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name_tr: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[str | None] = mapped_column(Text)
    parent_sector_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("ref.sector.sector_id"),
    )


class EntitySector(Base):
    __tablename__ = "entity_sector"
    __table_args__ = ({"schema": "ref"},)

    entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ref.entity.entity_id"),
        primary_key=True,
    )
    sector_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("ref.sector.sector_id"),
        primary_key=True,
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    valid_from: Mapped[date] = mapped_column(
        Date,
        primary_key=True,
        server_default=text("'1900-01-01'"),
    )
    valid_to: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        server_default=text("'9999-12-31'"),
    )


class Calendar(Base):
    __tablename__ = "calendar"
    __table_args__ = ({"schema": "ref"},)

    calendar_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="Europe/Istanbul")


class CalendarDay(Base):
    __tablename__ = "calendar_day"
    __table_args__ = ({"schema": "ref"},)

    calendar_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("ref.calendar.calendar_id"),
        primary_key=True,
    )
    dt: Mapped[date] = mapped_column(Date, primary_key=True)
    is_session: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Pragmatic v0.1 shortcut: TIME columns surfaced as Optional[str] in the ORM.
    # The migration creates real TIME columns; no consumer reads these yet.
    session_open: Mapped[str | None] = mapped_column(Text)
    session_close: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class Currency(Base):
    __tablename__ = "currency"
    __table_args__ = ({"schema": "ref"},)

    currency_code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    minor_unit: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("2"))
