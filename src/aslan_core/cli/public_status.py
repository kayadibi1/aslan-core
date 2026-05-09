"""``aslan public-status`` Click group — NG1.

The public status page ships a single subcommand:

* :command:`serve` — start the FastHTML public-status app via uvicorn.
  Like the internal dashboard's CLI it defaults to a loopback bind
  address; non-loopback hosts require ``--i-know-this-is-unsafe``.

The CLI deliberately does NOT call ``set_actor`` on the top-level
group's Actor: the public-status process logs in as
``public_status_reader`` which has no INSERT privilege anywhere — an
actor would have nowhere to land.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import click

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def _resolve_serve() -> Callable[..., Any]:
    """Late-resolve ``aslan_core.public_status.serve`` so monkeypatch
    targets land. Importing at module-import time would freeze the
    binding before tests run.
    """
    from aslan_core import public_status as _public_status

    return _public_status.serve


@click.group("public-status")
def public_status() -> None:
    """Public, no-auth ``/status`` page (NG1)."""


@public_status.command()
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address. Non-loopback hosts require --i-know-this-is-unsafe.",
)
@click.option(
    "--port",
    default=8081,
    show_default=True,
    type=int,
    help="TCP port to listen on.",
)
@click.option(
    "--i-know-this-is-unsafe",
    "unsafe_ack",
    is_flag=True,
    default=False,
    help=(
        "Acknowledge that --host is a non-loopback address. The public "
        "status page is intended to be fronted by a CDN / reverse proxy "
        "(DNS: status.aslan.<domain> -> :8081). Binding directly to a "
        "public interface without a fronting layer skips request rate "
        "limiting + structured access logging."
    ),
)
def serve(*, host: str, port: int, unsafe_ack: bool) -> None:
    """Start the public-status page via uvicorn.

    Reads ``ASLAN_PUBLIC_STATUS_DSN`` for the dedicated
    ``public_status_reader`` PostgreSQL role created in migration 0063.
    Without that env var the CLI exits with a usage error pointing at
    the migration — operators recover with ``alembic upgrade head``
    plus an env var.

    Recommended deployment: dedicated container with a read-only DB
    user (``public_status_reader``), DNS ``status.aslan.<domain>``
    pointed at the listening port, optional CDN in front
    (``Cache-Control: public, max-age=60`` already supports it),
    external uptime check (UptimeRobot / similar) at 30-second
    interval.
    """
    if "ASLAN_PUBLIC_STATUS_DSN" not in os.environ:
        raise click.UsageError(
            "ASLAN_PUBLIC_STATUS_DSN is unset. The public-status page "
            "requires the dedicated public_status_reader PostgreSQL role "
            "created in migration 0063 (USAGE on schema audit + SELECT on "
            "audit.recency_observation + audit.coverage_snapshot only). "
            "Run `alembic upgrade head` and export "
            "ASLAN_PUBLIC_STATUS_DSN=postgresql://public_status_reader:"
            "<password>@<host>/<db>."
        )

    if not _is_loopback(host) and not unsafe_ack:
        raise click.UsageError(
            f"--host {host!r} is not a loopback address. The public-status "
            "page is intended to be fronted by a CDN / reverse proxy. "
            "If you have one in place (TLS termination + structured "
            "access logging + rate limiting), re-run with "
            "--i-know-this-is-unsafe."
        )

    serve_fn = _resolve_serve()
    serve_fn(host=host, port=port)
