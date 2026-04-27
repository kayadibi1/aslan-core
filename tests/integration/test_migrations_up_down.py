import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.integration


def test_migrations_round_trip(pg_dsn: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_dsn)
    # The autouse fixture already brought us to head; verify down→up works.
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
