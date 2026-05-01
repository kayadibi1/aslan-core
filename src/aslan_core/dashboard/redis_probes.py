"""Bounded Redis probes. Filled in Task 6 of the v0.6.0 plan.

O(1) probes only with 200ms timeout, per-page call budget, and a
circuit breaker that returns ``unknown`` after 5 errors / 60s. No
per-row XRANGE — the deadletter page renders ``redis_state`` from
the bounded probe set.
"""

from __future__ import annotations
