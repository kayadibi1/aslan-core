"""Verify the 32-value agg.filing_event_type ENUM exists post-migration."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


_EXPECTED_VALUES: tuple[str, ...] = (
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


async def test_agg_filing_event_type_enum_has_32_values(session: AsyncSession) -> None:
    rows = (
        (
            await session.execute(
                sa.text(
                    """
                SELECT enumlabel
                FROM pg_enum e
                JOIN pg_type t ON t.oid = e.enumtypid
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname = 'agg' AND t.typname = 'filing_event_type'
                ORDER BY e.enumsortorder
                """
                )
            )
        )
        .scalars()
        .all()
    )
    assert tuple(rows) == _EXPECTED_VALUES
    assert len(rows) == 32
