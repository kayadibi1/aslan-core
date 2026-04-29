from __future__ import annotations

import click

from aslan_core.cli.doc import doc
from aslan_core.cli.migrate import migrate
from aslan_core.cli.registry import registry
from aslan_core.cli.seed import seed


@click.group()
def cli() -> None:
    """aslan — operations CLI for the aslan-core platform."""


cli.add_command(migrate)
cli.add_command(seed)
cli.add_command(registry)
cli.add_command(doc)


if __name__ == "__main__":
    cli()
