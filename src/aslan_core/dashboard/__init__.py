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


def serve(*, host: str = "127.0.0.1", port: int = 8585, reload: bool = False) -> None:
    """Start the FastHTML dashboard via uvicorn.

    Implementation lands in Task 12 of the v0.6.0 plan. The signature
    is locked here so Task 3's import test can pin it.
    """
    raise NotImplementedError("serve() is implemented in Task 12 of the v0.6.0 plan")


__all__ = ["serve"]
