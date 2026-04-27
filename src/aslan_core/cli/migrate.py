from __future__ import annotations

from pathlib import Path

import click
from alembic import command
from alembic.config import Config


def _config() -> Config:
    return Config(str(Path("alembic.ini")))


@click.group()
def migrate() -> None:
    """Database migrations."""


@migrate.command("up")
def migrate_up() -> None:
    """Upgrade to head."""
    command.upgrade(_config(), "head")


@migrate.command("revision")
@click.option("-m", "--message", required=True)
def migrate_revision(message: str) -> None:
    """Create a new revision file."""
    command.revision(_config(), message=message, autogenerate=False)


@migrate.command("history")
def migrate_history() -> None:
    """List revisions."""
    command.history(_config())


@migrate.command("downgrade")
@click.argument("revision")
def migrate_downgrade(revision: str) -> None:
    """Downgrade to <revision>. Discouraged in prod."""
    click.echo(f"WARNING: downgrade is destructive. Target: {revision}", err=True)
    command.downgrade(_config(), revision)
