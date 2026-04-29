from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.asyncio(loop_scope="session")
async def test_doc_schema_exists(session: AsyncSession) -> None:
    """After migrations apply, the 'doc' schema exists."""
    result = await session.scalar(
        text("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'doc'")
    )
    assert result == 1
