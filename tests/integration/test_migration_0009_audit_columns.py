"""Integration test: migration 0009 — nullable audit columns.

Confirms every mutation table has the five expected audit columns,
that they are nullable in v0.3 (per spec §2 / migration 0009 docstring),
and that src.ingestion_run has the run-only subset.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

# Codex F2 (2026-04-29) — src.watermark IS in the full-attribution list.
# Codex Batch 2 F1 (2026-04-29) — ref.sector IS a public mutation target
# (upsert_sector creates/updates rows), so it gets full audit columns too.
_FULL_TABLES: tuple[tuple[str, str], ...] = (
    ("ref", "entity"),
    ("ref", "identifier"),
    ("ref", "entity_sector"),
    ("ref", "entity_relationship"),
    ("ref", "sector"),
    ("doc", "filing"),
    ("doc", "filing_attachment"),
    ("doc", "filing_body"),
    ("src", "watermark"),
)

_FULL_AUDIT_COLUMNS: tuple[str, ...] = (
    "actor_id",
    "actor_kind",
    "client_ip",
    "user_agent",
    "request_id",
)


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_columns_present_and_nullable(session: AsyncSession) -> None:
    for schema, table in _FULL_TABLES:
        cols = (
            await session.execute(
                text(
                    "SELECT column_name, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = :t"
                ),
                {"s": schema, "t": table},
            )
        ).all()
        names = {c.column_name: c.is_nullable for c in cols}
        for col in _FULL_AUDIT_COLUMNS:
            assert col in names, f"{schema}.{table} missing audit column {col!r}"
            assert names[col] == "YES", (
                f"{schema}.{table}.{col} should be nullable in v0.3 "
                f"(got is_nullable={names[col]!r})"
            )


@pytest.mark.asyncio(loop_scope="session")
async def test_run_table_has_only_actor_columns(session: AsyncSession) -> None:
    cols = (
        await session.execute(
            text(
                "SELECT column_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'src' AND table_name = 'ingestion_run'"
            )
        )
    ).all()
    names = {c.column_name: c.is_nullable for c in cols}
    assert names.get("actor_id") == "YES"
    assert names.get("actor_kind") == "YES"
    # Per-call fields do not apply to a per-run row.
    for col in ("client_ip", "user_agent", "request_id"):
        assert col not in names, f"src.ingestion_run should NOT have {col!r} (per-call only)"


@pytest.mark.asyncio(loop_scope="session")
async def test_actor_kind_check_constraint_present_on_every_table(
    session: AsyncSession,
) -> None:
    """Every audit-column-bearing table has a CHECK constraint on
    actor_kind that pins it to ('user','service','system'). The
    constraint is what stops a programming error from smuggling a
    bogus kind into the data plane."""
    all_audit_tables = [*_FULL_TABLES, ("src", "ingestion_run")]
    for schema, table in all_audit_tables:
        rows = (
            await session.execute(
                text(
                    "SELECT pg_get_constraintdef(c.oid) AS def "
                    "FROM pg_constraint c "
                    "JOIN pg_class t ON c.conrelid = t.oid "
                    "JOIN pg_namespace n ON t.relnamespace = n.oid "
                    "WHERE n.nspname = :s AND t.relname = :t "
                    "  AND c.contype = 'c'"
                ),
                {"s": schema, "t": table},
            )
        ).all()
        defs = [r.def_ if hasattr(r, "def_") else r[0] for r in rows]
        # Each CHECK definition is a string like:
        #   CHECK (actor_kind = ANY (ARRAY['user'::text, 'service'::text, 'system'::text]))
        actor_kind_checks = [d for d in defs if d and "actor_kind" in d]
        assert actor_kind_checks, f"{schema}.{table} has no CHECK constraint mentioning actor_kind"
        joined = " ".join(actor_kind_checks)
        for kind in ("user", "service", "system"):
            assert kind in joined, (
                f"{schema}.{table} actor_kind CHECK is missing {kind!r}; got {joined!r}"
            )
