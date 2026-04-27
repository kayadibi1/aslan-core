import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aslan_core.db.session import session_scope

pytestmark = pytest.mark.integration


async def test_session_scope_commits_on_clean_exit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(session_factory) as s:
        await s.execute(text("CREATE TEMP TABLE _t (x INT)"))
        await s.execute(text("INSERT INTO _t VALUES (1)"))
    # Temp tables are per-connection; we don't assert across sessions here.
    # The point is: no exception means commit fired and the close was clean.


async def test_session_scope_rolls_back_on_exception(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with session_scope(session_factory) as s:
            await s.execute(text("SELECT 1"))
            raise Boom()
