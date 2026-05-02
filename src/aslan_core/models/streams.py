"""SQLAlchemy ORM mappings for the ``streams.*`` tables.

INTERNAL — not part of the public API per CLAUDE.md. Tasks 8-18 import
these to construct INSERTs against the migrated schema; consumers of
aslan-core should use ``aslan_core.streams.{StreamProducer,
StreamConsumer, ...}`` instead.

The mappings faithfully mirror migrations 0016-0019 (Tasks 4-5c). When
a migration changes the on-disk schema, the matching ORM class MUST be
updated in lockstep.
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
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from aslan_core.models import Base


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        Index(
            "outbox_pending",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
        Index("outbox_event_id", "event_id"),
        Index("outbox_stream_name", "stream_name", "created_at"),
        Index("outbox_run", "producer_run_id"),
        {"schema": "streams"},
    )

    outbox_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stream_name: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, unique=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    producer_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("src.ingestion_run.ingestion_run_id"),
        nullable=False,
    )
    source_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("src.source.source_id"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redis_message_id: Mapped[str | None] = mapped_column(Text)
    publish_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[str | None] = mapped_column(Text)
    actor_kind: Mapped[str | None] = mapped_column(
        Text,
        CheckConstraint(
            "actor_kind IN ('user','service','system')",
            name="outbox_actor_kind_check",
        ),
    )
    client_ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))


class DeadletterLog(Base):
    __tablename__ = "deadletter_log"
    __table_args__ = (
        UniqueConstraint(
            "stream_name",
            "group_name",
            "original_message_id",
            name="deadletter_routing_uq",
        ),
        Index("deadletter_event_id", "event_id"),
        Index("deadletter_stream_routed", "stream_name", "routed_at"),
        Index(
            "deadletter_pending_redis",
            "failure_id",
            postgresql_where=text("routed_at_redis IS NULL"),
        ),
        {"schema": "streams"},
    )

    failure_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stream_name: Mapped[str] = mapped_column(Text, nullable=False)
    deadletter_stream: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    original_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    group_name: Mapped[str] = mapped_column(Text, nullable=False)
    consumer_name: Mapped[str] = mapped_column(Text, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, nullable=False)
    payload_excerpt: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    routed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    routed_at_redis: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redis_message_id: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[str | None] = mapped_column(Text)
    actor_kind: Mapped[str | None] = mapped_column(
        Text,
        CheckConstraint(
            "actor_kind IN ('user','service','system')",
            name="deadletter_log_actor_kind_check",
        ),
    )
    request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))


class DeadletterRedisIndex(Base):
    __tablename__ = "deadletter_redis_index"
    __table_args__ = ({"schema": "streams"},)

    failure_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("streams.deadletter_log.failure_id", ondelete="CASCADE"),
        primary_key=True,
    )
    redis_message_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class DeadletterXaddIntent(Base):
    __tablename__ = "deadletter_xadd_intent"
    __table_args__ = (
        Index("deadletter_xadd_intent_stale", "intent_at"),
        Index("deadletter_xadd_intent_heartbeat", "owner_heartbeat_at"),
        {"schema": "streams"},
    )

    failure_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("streams.deadletter_log.failure_id", ondelete="CASCADE"),
        primary_key=True,
    )
    stream_name: Mapped[str] = mapped_column(Text, nullable=False)
    intent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    redis_lower_bound_id: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    owner_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


class RedactionRegistry(Base):
    __tablename__ = "redaction_registry"
    __table_args__ = (
        Index("redaction_registry_redacted_at", "redacted_at"),
        Index("redaction_registry_stream", "original_stream"),
        {"schema": "streams"},
    )

    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    redaction_reason: Mapped[str] = mapped_column(Text, nullable=False)
    redacted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    original_stream: Mapped[str] = mapped_column(Text, nullable=False)
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    redacted_payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    original_payload_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(Text)
    actor_kind: Mapped[str | None] = mapped_column(
        Text,
        CheckConstraint(
            "actor_kind IN ('user','service','system')",
            name="redaction_registry_actor_kind_check",
        ),
    )
    request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))


class EventIdToRedis(Base):
    __tablename__ = "event_id_to_redis"
    __table_args__ = (
        Index("event_id_to_redis_event_id", "event_id"),
        Index(
            "event_id_to_redis_pending",
            "stream_name",
            "published_at",
            postgresql_where=text("redacted_at IS NULL"),
        ),
        {"schema": "streams"},
    )

    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    stream_name: Mapped[str] = mapped_column(Text, primary_key=True)
    redis_message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "DeadletterLog",
    "DeadletterRedisIndex",
    "DeadletterXaddIntent",
    "EventIdToRedis",
    "Outbox",
    "RedactionRegistry",
]
