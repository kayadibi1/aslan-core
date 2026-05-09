"""NG1 — public, no-auth ``/status`` page for the dq subsystem.

Sibling to :mod:`aslan_core.dashboard`, served as a separate FastHTML
app on a separate port. Reads ONLY from ``audit.recency_observation``
and ``audit.coverage_snapshot`` via the dedicated
``public_status_reader`` PostgreSQL role created in migration 0063.

Privilege boundary (defense-in-depth):

  * The role lacks USAGE on every schema except ``audit``;
  * The role has SELECT on exactly two tables;
  * ``default_transaction_read_only=on`` is the soft floor.

A misconfigured public-status process literally cannot reach
``streams.outbox.payload``, ``doc.filing_body``, ``audit.events``,
``audit.alert_dispatch.payload``, or any other internal surface
even if a future code path tries to query them.

The page is forward-compatible — the schema reads only from existing
tables, no new columns. When paying users justify deeper trust
commitments (incident history, postmortems, subscriptions) those
features can layer on without DB changes.

The package is gated by the ``aslan-core[dashboard]`` extra (same
as the internal dashboard — both depend on python-fasthtml + uvicorn).
"""

from __future__ import annotations

import os


def serve(*, host: str = "127.0.0.1", port: int = 8081) -> None:
    """Start the public status page via uvicorn.

    Reads ``ASLAN_PUBLIC_STATUS_DSN`` for the dedicated
    ``public_status_reader`` role's connection string. Read at call
    time (not at module import) so a test that monkeypatches
    ``serve`` runs without the env var.

    The CLI subcommand (``aslan public-status serve``) validates the
    bind address and the DSN env var BEFORE this function runs, so
    the body here proceeds unconditionally.

    Imports inside the function body so consumers without the
    ``[dashboard]`` extra installed can ``from aslan_core import
    public_status`` without pulling python-fasthtml or uvicorn.
    """
    import uvicorn

    from aslan_core.db.engine import create_engine
    from aslan_core.db.session import create_session_factory
    from aslan_core.public_status.app import app, configure_app

    dsn = os.environ.get("ASLAN_PUBLIC_STATUS_DSN")
    if dsn is None:
        # Defensive — the CLI checks for this before calling serve(),
        # but a programmatic caller could skip the check.
        raise RuntimeError("ASLAN_PUBLIC_STATUS_DSN is not set")

    engine = create_engine(dsn=dsn)
    session_factory = create_session_factory(engine)
    configure_app(session_factory=session_factory)

    uvicorn.run(app, host=host, port=port)


__all__ = ["serve"]
