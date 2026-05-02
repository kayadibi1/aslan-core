"""v0.6.0 internal ops dashboard.

Read-only operator-facing FastHTML web UI for the aslan-core data
plane. See ``crawl/plans/aslan-core-v0.6.0-dashboard.md`` for the
full spec — the dashboard logs in to PostgreSQL as the dedicated
``aslan_dashboard`` role (created in migration 0020) so the column-
allowlist GRANTs and ``default_transaction_read_only=on`` floor are
the load-bearing GDPR + read-only guarantees, not the in-process
view-model checks (which exist as defense-in-depth).

The package is gated by the ``aslan-core[dashboard]`` extra — base
installs do not pull ``python-fasthtml`` or ``uvicorn``. Importing
this subpackage without the extra installed raises ``ImportError``
at the first ``serve()`` invocation, not at import time, so unit
tests that only inspect signatures continue to work.
"""

from __future__ import annotations

import os


def serve(*, host: str = "127.0.0.1", port: int = 8585) -> None:
    """Start the FastHTML dashboard via uvicorn.

    Reads ``ASLAN_DASHBOARD_DSN`` for the dedicated ``aslan_dashboard``
    role's connection string and ``ASLAN_REDIS_URL`` for the Redis
    client. Both are read at call time (not at module import) so a
    test that monkeypatches ``serve`` runs without the env vars.

    The CLI subcommand (`aslan dashboard serve`) validates the bind
    address and the DSN env var BEFORE this function runs, so the
    body here proceeds unconditionally — operators who construct
    custom embeddings (rare; the v0.6.0 surface is CLI-driven) are
    on the hook for setting up an isolated process and a fronting
    proxy themselves.

    No ``reload`` parameter: ultrareview bug_005 found that
    uvicorn's auto-reload requires an import string + a worker
    subprocess that re-runs ``configure_app``. Wiring that up
    properly is real work and the feature is dev-only ergonomics;
    it has been dropped from the CLI until a later release adds
    the proper plumbing.

    Imports inside the function body so consumers without the
    ``[dashboard]`` extra installed can ``from aslan_core import
    dashboard`` without pulling python-fasthtml or uvicorn.
    """
    import uvicorn
    from redis.asyncio import Redis

    from aslan_core.dashboard.app import app, configure_app
    from aslan_core.db.engine import create_engine
    from aslan_core.db.session import create_session_factory

    dsn = os.environ.get("ASLAN_DASHBOARD_DSN")
    redis_url = os.environ.get("ASLAN_REDIS_URL", "redis://127.0.0.1:6379/0")
    if dsn is None:
        # Defensive — the CLI checks for this before calling serve(),
        # but a programmatic caller could skip the check.
        raise RuntimeError("ASLAN_DASHBOARD_DSN is not set")

    # The dashboard engine connects as the aslan_dashboard role; its
    # default_transaction_read_only=on at role level (migration 0020)
    # is the load-bearing read-only floor, NOT the local-engine kwargs.
    # We deliberately do not echo the DSN — `Settings.repr` would, but
    # we read straight from env.
    engine = create_engine(dsn=dsn)
    session_factory = create_session_factory(engine)
    redis = Redis.from_url(redis_url)
    configure_app(session_factory=session_factory, redis_client=redis)

    uvicorn.run(app, host=host, port=port)


__all__ = ["serve"]
