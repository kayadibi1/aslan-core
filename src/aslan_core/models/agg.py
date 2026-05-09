from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB
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


# ── filing_event taxonomy (M0 of aslan-event-extractor) ──────────


_FILING_EVENT_TYPES: tuple[str, ...] = (
    "dividend",
    "dividend_revision",
    "capital_action",
    "capital_action_revision",
    "share_buyback",
    "rights_offering",
    "treasury_share_change",
    "prospectus_published",
    "name_change",
    "delisting",
    "merger_acquisition",
    "tender_offer",
    "divestiture",
    "articles_amendment",
    "bankruptcy_event",
    "board_change",
    "executive_change",
    "auditor_change",
    "insider_trade",
    "material_shareholder_change",
    "earnings_release",
    "guidance_update",
    "material_contract",
    "material_contract_termination",
    "litigation_update",
    "regulatory_action",
    "going_concern",
    "force_majeure",
    "credit_rating_change",
    "general_assembly",
    "esg_disclosure",
    "other_material",
)
"""32-value enum, mirrored from migration 0034. Application code that
classifies an event MUST match one of these strings exactly. Adding a
new value is a schema change (D24)."""


_filing_event_type = PG_ENUM(
    *_FILING_EVENT_TYPES,
    name="filing_event_type",
    schema="agg",
    create_type=False,
)


class FilingEvent(Base):
    """Typed corporate-action / material-event row.

    Bitemporal: every state change is a NEW row with NEW ``as_of``;
    the prior row gets ``superseded_at = new.as_of``. Replayability
    invariants live in the migration's docstring (M5 replay test).
    """

    __tablename__ = "filing_event"
    __table_args__ = (
        UniqueConstraint(
            "filing_id",
            "event_type",
            "event_seq",
            "as_of",
            name="fe_natural_key",
        ),
        CheckConstraint(
            "primary_confidence >= 0.0 AND primary_confidence <= 1.0",
            name="fe_primary_conf_range",
        ),
        CheckConstraint(
            "final_confidence >= 0.0 AND final_confidence <= 1.0",
            name="fe_final_conf_range",
        ),
        CheckConstraint("event_seq >= 1", name="fe_event_seq_pos"),
        {"schema": "agg"},
    )

    filing_event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    filing_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("doc.filing.filing_id"),
        nullable=False,
    )
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_filing_ref: Mapped[str] = mapped_column(Text, nullable=False)

    event_type: Mapped[str] = mapped_column(_filing_event_type, nullable=False)
    event_seq: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)

    entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ref.entity.entity_id"),
        nullable=False,
    )
    counterparty_entity_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ref.entity.entity_id"),
        nullable=True,
    )

    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_dt: Mapped[date | None] = mapped_column(Date, nullable=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    primary_model_version: Mapped[str] = mapped_column(Text, nullable=False)
    primary_prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    primary_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    verifier_model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    verifier_prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    verifier_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    verifier_agreement: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    final_confidence: Mapped[float] = mapped_column(Float, nullable=False)

    input_text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_text_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    input_token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    ingestion_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("src.ingestion_run.ingestion_run_id"),
        nullable=False,
    )
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FilingEventQuarantine(Base):
    """Bodies the extractor cannot process. Operator-actioned via /review."""

    __tablename__ = "filing_event_quarantine"
    __table_args__ = ({"schema": "agg"},)

    filing_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("doc.filing.filing_id"),
        primary_key=True,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FilingEventReviewQueue(Base):
    """Tier 1/2 disagreements awaiting human resolution."""

    __tablename__ = "filing_event_review_queue"
    __table_args__ = ({"schema": "agg"},)

    review_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    filing_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("doc.filing.filing_id"),
        nullable=False,
    )
    primary_event_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    primary_model: Mapped[str] = mapped_column(Text, nullable=False)
    primary_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    primary_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    verifier_model: Mapped[str] = mapped_column(Text, nullable=False)
    verifier_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    verifier_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    diff_summary: Mapped[str] = mapped_column(Text, nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class FilingEventExtractionState(Base):
    """One row per (filing, model_version, prompt_version) — drives re-extraction."""

    __tablename__ = "filing_event_extraction_state"
    __table_args__ = ({"schema": "agg"},)

    filing_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("doc.filing.filing_id"),
        primary_key=True,
    )
    model_version: Mapped[str] = mapped_column(Text, primary_key=True)
    prompt_version: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    last_extracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class FilingEventPiiIndex(Base):
    """GDPR Art. 17 PII path index.

    NOT granted to ``aslan_dashboard`` — privileged only. This row exists
    once per (filing_event_id, payload_path) pair; redactions update it
    in place and write an audit row.
    """

    __tablename__ = "filing_event_pii_index"
    __table_args__ = ({"schema": "agg"},)

    filing_event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agg.filing_event.filing_event_id", ondelete="CASCADE"),
        primary_key=True,
    )
    pii_kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload_path: Mapped[str] = mapped_column(Text, primary_key=True)
    redacted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redaction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class EntityResolutionQueue(Base):
    """Async-resolved counterparty / mentioned-entity backlog."""

    __tablename__ = "entity_resolution_queue"
    __table_args__ = ({"schema": "agg"},)

    queue_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("agg.filing_event.filing_event_id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_name: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    payload_path: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_entity_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ref.entity.entity_id"),
        nullable=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExtractionAuditLog(Base):
    """Every LLM call audited (SOC II Processing Integrity).

    Hypertable; PK is composite ``(audit_id, started_at)`` because
    TimescaleDB requires the partitioning column in the PK.
    """

    __tablename__ = "extraction_audit_log"
    __table_args__ = ({"schema": "agg"},)

    audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    filing_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    pass_kind: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    output_token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_micro_usd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    ingestion_run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class FilingEventLabel(Base):
    """Ground-truth labels for the 500-disclosure benchmark set (D20)."""

    __tablename__ = "filing_event_label"
    __table_args__ = ({"schema": "agg"},)

    label_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filing_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("doc.filing.filing_id"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(_filing_event_type, nullable=False)
    event_seq: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    expected_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    labeller: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_holdout: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    labelled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
