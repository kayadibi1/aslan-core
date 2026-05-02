"""``aslan dashboard`` Click group — Task 12.

The v0.6.0 dashboard ships a single subcommand:

* :command:`serve` — start the FastHTML app via uvicorn, bound to
  the loopback interface by default. Non-loopback hosts require the
  explicit ``--i-know-this-is-unsafe`` flag because the localhost /
  SSH-tunnel deployment is the only shape with a defined compliance
  posture in v0.6.0; binding to ``0.0.0.0`` puts the surface on the
  network without the reverse-proxy story (mTLS, SSO header,
  structured access log, integrity-protected log shipping).

The CLI deliberately does NOT call ``set_actor`` on the
top-level group's Actor: the dashboard's PostgreSQL role lacks
``INSERT`` privilege on ``audit.events``, so an actor would have
nowhere to land. Spec §6.2: who-looked-at-what is captured at the
network edge, not in the application audit trail.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import click

# ``aslan_core.dashboard.serve`` is looked up at command-invocation
# time (not at module-import time) so tests can monkeypatch it
# without having to reload modules.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def _resolve_serve() -> Callable[..., Any]:
    """Late-resolve ``aslan_core.dashboard.serve`` so monkeypatch
    targets land. Importing at module-import time would freeze the
    binding before tests run."""
    from aslan_core import dashboard as _dashboard

    return _dashboard.serve


@click.group()
def dashboard() -> None:
    """Operator-facing v0.6.0 internal ops dashboard."""


@dashboard.command()
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address. Non-loopback hosts require --i-know-this-is-unsafe.",
)
@click.option(
    "--port",
    default=8585,
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
        "Acknowledge that --host is a non-loopback address. The dashboard "
        "has no per-request authentication in v0.6.0; binding to a public "
        "interface without a reverse-proxy fronting it (mTLS or SSO header, "
        "structured access log, integrity-protected log shipping) puts "
        "operator data within reach of any caller on the network."
    ),
)
def serve(*, host: str, port: int, unsafe_ack: bool) -> None:
    """Start the dashboard via uvicorn.

    Reads ``ASLAN_DASHBOARD_DSN`` for the dedicated ``aslan_dashboard``
    PostgreSQL role created in migration 0020. Without that env var
    the CLI exits with a usage error pointing at the migration —
    operators recover with ``alembic upgrade head`` plus an env var.

    Note on ``--reload``: ultrareview bug_005 surfaced that
    uvicorn's auto-reload requires the application as an import
    string AND a worker subprocess that re-runs ``configure_app``.
    Wiring that up correctly is meaningful work and the flag is
    dev-only ergonomics; it has been removed entirely until a
    later release adds the proper plumbing. Developers can restart
    the process manually in the meantime.
    """
    if "ASLAN_DASHBOARD_DSN" not in os.environ:
        raise click.UsageError(
            "ASLAN_DASHBOARD_DSN is unset. The dashboard requires the "
            "dedicated aslan_dashboard PostgreSQL role created in migration "
            "0020 (column-allowlist GRANTs + default_transaction_read_only). "
            "Run `alembic upgrade head` and export "
            "ASLAN_DASHBOARD_DSN=postgresql://aslan_dashboard:<password>@<host>/<db>."
        )

    if not _is_loopback(host) and not unsafe_ack:
        raise click.UsageError(
            f"--host {host!r} is not a loopback address. The v0.6.0 "
            "dashboard has no per-request authentication; binding to a "
            "public interface without a fronting reverse-proxy is unsafe. "
            "If you have a proxy doing mTLS or SSO header injection plus "
            "structured access logging, re-run with --i-know-this-is-unsafe."
        )

    serve_fn = _resolve_serve()
    serve_fn(host=host, port=port)
