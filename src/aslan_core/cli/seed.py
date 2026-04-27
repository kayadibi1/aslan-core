from __future__ import annotations

import click


@click.group()
def seed() -> None:
    """Seed reference data."""
