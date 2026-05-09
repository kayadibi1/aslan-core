"""Verify agg.extraction_audit_log — hypertable, columns, compression policy, no grant."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_audit_log_is_hypertable(session: AsyncSession) -> None:
    count = (
        await session.execute(
            sa.text(
                "SELECT count(*) FROM timescaledb_information.hypertables "
                "WHERE hypertable_schema='agg' AND hypertable_name='extraction_audit_log'"
            )
        )
    ).scalar()
    assert count == 1


async def test_audit_log_columns(session: AsyncSession) -> None:
    cols = {
        r[0]
        for r in (
            await session.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='agg' AND table_name='extraction_audit_log'"
                )
            )
        ).all()
    }
    expected = {
        "audit_id",
        "filing_id",
        "pass_kind",
        "model_version",
        "prompt_version",
        "prompt_sha256",
        "input_text_sha256",
        "input_token_count",
        "output_token_count",
        "output_sha256",
        "output_storage_key",
        "latency_ms",
        "cost_micro_usd",
        "actor_id",
        "ingestion_run_id",
        "started_at",
        "finished_at",
        "error",
    }
    assert expected <= cols


async def test_audit_log_compression_settings(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            sa.text(
                "SELECT count(*) FROM timescaledb_information.compression_settings "
                "WHERE hypertable_schema='agg' AND hypertable_name='extraction_audit_log'"
            )
        )
    ).scalar()
    assert rows is not None and rows >= 1


async def test_audit_log_not_granted_to_dashboard(session: AsyncSession) -> None:
    has_priv = (
        await session.execute(
            sa.text(
                "SELECT has_table_privilege('aslan_dashboard', "
                "'agg.extraction_audit_log', 'SELECT')"
            )
        )
    ).scalar()
    assert has_priv is False, (
        "agg.extraction_audit_log must remain ungranted to aslan_dashboard (privileged audit log)."
    )
