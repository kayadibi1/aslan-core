from __future__ import annotations

import click


@click.group()
def registry() -> None:
    """Registry reads and mutations."""
