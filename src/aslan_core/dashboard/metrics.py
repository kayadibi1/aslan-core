"""Dashboard-specific Prometheus metrics. Filled in Task 14 of the v0.6.0 plan.

Closed-enum path labels with ``<sensitive>`` bucketing for
``/audit`` + ``/redactions`` (codex round-5). Request-duration histogram
unlabeled — no per-route timing leak (codex round-4). All four metrics
go through the existing ``aslan_core.observability.metrics`` allow-list.
"""

from __future__ import annotations
