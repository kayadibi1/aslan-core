"""Verify agg.filing_event table, indexes, view + constraints."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_agg_filing_event_columns(session: AsyncSession) -> None:
    cols = {
        r[0]: r[1]
        for r in (
            await session.execute(
                sa.text(
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'agg' AND table_name = 'filing_event'
                    """
                )
            )
        ).all()
    }
    expected = {
        "filing_event_id": "uuid",
        "filing_id": "uuid",
        "source_id": "text",
        "source_filing_ref": "text",
        "event_type": "USER-DEFINED",
        "event_seq": "smallint",
        "entity_id": "uuid",
        "counterparty_entity_id": "uuid",
        "event_ts": "timestamp with time zone",
        "effective_dt": "date",
        "as_of": "timestamp with time zone",
        "superseded_at": "timestamp with time zone",
        "payload": "jsonb",
        "primary_model_version": "text",
        "primary_prompt_version": "text",
        "primary_confidence": "real",
        "verifier_model_version": "text",
        "verifier_prompt_version": "text",
        "verifier_confidence": "real",
        "verifier_agreement": "boolean",
        "final_confidence": "real",
        "input_text_sha256": "character",
        "input_text_chars": "integer",
        "input_token_count": "integer",
        "output_token_count": "integer",
        "ingestion_run_id": "bigint",
        "extracted_at": "timestamp with time zone",
    }
    for k, expected_type in expected.items():
        assert k in cols, f"missing column {k}"
        actual = cols[k]
        # CHAR(64) reports as 'character'; smallint/integer/bigint are exact.
        assert actual == expected_type or actual.startswith(expected_type), (
            f"{k}: got {actual!r}, expected {expected_type!r}"
        )


async def test_agg_filing_event_indexes(session: AsyncSession) -> None:
    rows = (
        (
            await session.execute(
                sa.text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname='agg' AND tablename='filing_event'"
                )
            )
        )
        .scalars()
        .all()
    )
    expected_idx = {
        "fe_entity_type_event_ts",
        "fe_filing",
        "fe_event_ts",
        "fe_source_ref",
        "fe_payload_gin",
        "fe_current_only",
        "fe_natural_key",
    }
    assert expected_idx.issubset(set(rows))


async def test_agg_filing_event_current_view(session: AsyncSession) -> None:
    rows = (
        (
            await session.execute(
                sa.text(
                    "SELECT viewname FROM pg_views "
                    "WHERE schemaname='agg' AND viewname='filing_event_current'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == ["filing_event_current"]


async def test_agg_filing_event_check_constraints(session: AsyncSession) -> None:
    rows = (
        (
            await session.execute(
                sa.text(
                    "SELECT conname FROM pg_constraint c "
                    "JOIN pg_class cl ON cl.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = cl.relnamespace "
                    "WHERE cl.relname='filing_event' AND n.nspname='agg' AND c.contype='c'"
                )
            )
        )
        .scalars()
        .all()
    )
    names = set(rows)
    assert {"fe_primary_conf_range", "fe_final_conf_range", "fe_event_seq_pos"} <= names
