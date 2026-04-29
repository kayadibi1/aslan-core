from __future__ import annotations

import getpass
import socket

import click

from aslan_core.audit import Actor, set_actor
from aslan_core.cli.doc import doc
from aslan_core.cli.migrate import migrate
from aslan_core.cli.registry import registry
from aslan_core.cli.seed import seed


@click.group()
@click.option(
    "--actor-id",
    default=None,
    hidden=True,
    help="Override the auto-derived CLI actor identity (advanced use only).",
)
@click.pass_context
def cli(ctx: click.Context, actor_id: str | None) -> None:
    """aslan — operations CLI for the aslan-core platform.

    Auto-sets the audit Actor from ``getpass.getuser()`` and
    ``socket.gethostname()`` so manual operations don't need a flag
    (Task 18, v0.3.0). Every CLI mutation lands an ``actor_id`` like
    ``cli:sidar@aslan-mbp.local`` in ``audit.events``. Pass
    ``--actor-id`` to override (e.g. when wrapped by a higher-level
    automation that already has its own identity).
    """
    if actor_id is None:
        actor_id = f"cli:{getpass.getuser()}@{socket.gethostname()}"
    set_actor(Actor(actor_id=actor_id, actor_kind="user"))


cli.add_command(migrate)
cli.add_command(seed)
cli.add_command(registry)
cli.add_command(doc)


if __name__ == "__main__":
    cli()
