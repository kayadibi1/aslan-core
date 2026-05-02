from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import BigInteger, Date, DateTime, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from aslan_core.models import Base


class RestatementConfig(Base):
    __tablename__ = "restatement_config"
    __table_args__ = ({"schema": "agg"},)

    config_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    cpi_series_code: Mapped[str] = mapped_column(Text, nullable=False)
    base_date: Mapped[date] = mapped_column(Date, nullable=False)
    applies_from: Mapped[date] = mapped_column(Date, nullable=False)
    applies_to: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_kind: Mapped[str | None] = mapped_column(Text, nullable=True)


class EntityLatestSnapshot(Base):
    __tablename__ = "entity_latest_snapshot"
    __table_args__ = ({"schema": "agg"},)

    entity_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    legal_name: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    bist_ticker: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_filing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filings_90d: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ObservationDailyToMonthly(Base):
    __tablename__ = "observation_daily_to_monthly"
    __table_args__ = ({"schema": "agg"},)

    series_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    month_end_value: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    month_avg_value: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    month_min: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    month_max: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    n_obs: Mapped[int] = mapped_column(BigInteger, nullable=False)
