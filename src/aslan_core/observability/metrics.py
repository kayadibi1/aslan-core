"""Prometheus metrics module — counters, histograms, and a gauge for
mutation-rate, latency, and lock-holder visibility.

Lazy-import contract: ``prometheus_client`` lives under the
``aslan-core[obs]`` install extra. A consumer that does not install
the extra must still be able to ``from aslan_core.observability
import metrics`` and call ``metrics.entity_creates.labels(...).inc()``
without an ``ImportError`` — in that case the call is a no-op.

Implementation:

  * :class:`_LazyCounter` / :class:`_LazyHistogram` / :class:`_LazyGauge`
    wrap a single prometheus_client primitive each. The underlying
    instance is created lazily on first use. If prometheus_client is
    not importable, the wrapper records the failure once and treats
    every subsequent call as a no-op.
  * Module-level constants (``entity_creates`` etc.) are the lazy
    handles. They expose the same surface (``.labels(...).inc()``,
    ``.labels(...).observe(x)``, ``.inc()`` / ``.dec()`` for the
    gauge) that prometheus_client offers.

Label cardinality is bounded — every label is either a small enum
(``kind``, ``actor_kind``, ``op``), a stringified bool (``created``,
``success``), or a low-cardinality identifier (``source_id``,
``bucket``, ``operation``). NO unbounded labels (no ``entity_id``,
no ``request_id``).
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)


# ── Lazy-handle scaffolding ──────────────────────────────────────────


class _NoopChild:
    """Returned by a lazy handle when prometheus_client is not importable.
    Exposes ``.inc()`` / ``.observe(x)`` / ``.dec()`` as no-ops."""

    def inc(self, amount: float = 1.0) -> None:
        return None

    def dec(self, amount: float = 1.0) -> None:
        return None

    def observe(self, value: float) -> None:
        return None


_NOOP_CHILD: _NoopChild = _NoopChild()


class _LazyMetric:
    """Common base — defers prometheus_client import until first
    ``.labels(...)`` (counters/histograms) or first ``.inc()``/``.dec()``
    (gauges). Once instantiated the underlying prom object is cached."""

    def __init__(
        self,
        *,
        kind: str,
        name: str,
        documentation: str,
        labelnames: tuple[str, ...] = (),
        buckets: tuple[float, ...] | None = None,
    ) -> None:
        self._kind = kind
        self._name = name
        self._doc = documentation
        self.labelnames: tuple[str, ...] = labelnames
        self._buckets = buckets
        self._impl: Any | None = None
        self._import_failed = False

    def _ensure_impl(self) -> Any | None:
        if self._impl is not None or self._import_failed:
            return self._impl
        try:
            import prometheus_client as pc
        except ImportError:
            # Log once, then treat every subsequent call as a no-op.
            _log.debug(
                "prometheus_client not installed; metric %r is a no-op "
                "(install the aslan-core[obs] extra to enable)",
                self._name,
            )
            self._import_failed = True
            return None
        if self._kind == "counter":
            self._impl = pc.Counter(self._name, self._doc, labelnames=self.labelnames)
        elif self._kind == "histogram":
            kwargs: dict[str, Any] = {"labelnames": self.labelnames}
            if self._buckets is not None:
                kwargs["buckets"] = self._buckets
            self._impl = pc.Histogram(self._name, self._doc, **kwargs)
        elif self._kind == "gauge":
            self._impl = pc.Gauge(self._name, self._doc, labelnames=self.labelnames)
        else:  # pragma: no cover — defensive
            raise ValueError(f"unknown metric kind {self._kind!r}")
        return self._impl


class _LazyCounter(_LazyMetric):
    def __init__(self, *, name: str, documentation: str, labelnames: tuple[str, ...] = ()) -> None:
        super().__init__(
            kind="counter",
            name=name,
            documentation=documentation,
            labelnames=labelnames,
        )

    def labels(self, **kw: str) -> Any:
        impl = self._ensure_impl()
        if impl is None:
            return _NOOP_CHILD
        return impl.labels(**kw)

    def inc(self, amount: float = 1.0) -> None:
        impl = self._ensure_impl()
        if impl is None or self.labelnames:
            # Unlabeled inc only makes sense when no labels are declared.
            return
        impl.inc(amount)


class _LazyHistogram(_LazyMetric):
    def __init__(
        self,
        *,
        name: str,
        documentation: str,
        labelnames: tuple[str, ...] = (),
        buckets: tuple[float, ...] | None = None,
    ) -> None:
        super().__init__(
            kind="histogram",
            name=name,
            documentation=documentation,
            labelnames=labelnames,
            buckets=buckets,
        )

    def labels(self, **kw: str) -> Any:
        impl = self._ensure_impl()
        if impl is None:
            return _NOOP_CHILD
        return impl.labels(**kw)

    def observe(self, value: float) -> None:
        impl = self._ensure_impl()
        if impl is None or self.labelnames:
            return
        impl.observe(value)


class _LazyGauge(_LazyMetric):
    def __init__(self, *, name: str, documentation: str, labelnames: tuple[str, ...] = ()) -> None:
        super().__init__(
            kind="gauge",
            name=name,
            documentation=documentation,
            labelnames=labelnames,
        )

    def labels(self, **kw: str) -> Any:
        impl = self._ensure_impl()
        if impl is None:
            return _NOOP_CHILD
        return impl.labels(**kw)

    def inc(self, amount: float = 1.0) -> None:
        impl = self._ensure_impl()
        if impl is None:
            return
        impl.inc(amount)

    def dec(self, amount: float = 1.0) -> None:
        impl = self._ensure_impl()
        if impl is None:
            return
        impl.dec(amount)


# ── Counters ─────────────────────────────────────────────────────────

entity_creates = _LazyCounter(
    name="aslan_entity_creates_total",
    documentation="Total ref.entity inserts on the fresh-create path.",
    labelnames=("source_id",),
)

entity_merge_required = _LazyCounter(
    name="aslan_entity_merge_required_total",
    documentation=(
        "create_entity calls that hit a cross-source identifier match "
        "and raised EntityMergeRequired."
    ),
    labelnames=("source_id",),
)

filing_puts = _LazyCounter(
    name="aslan_filing_puts_total",
    documentation=(
        "DocumentStore.put_filing calls. created=true|false distinguishes "
        "fresh INSERT vs hash-dedup hit (case A or B)."
    ),
    labelnames=("source_id", "kind", "created"),
)

filing_releases = _LazyCounter(
    name="aslan_filing_releases_total",
    documentation=(
        "DocumentStore.release calls. success=true|false reflects whether "
        "every blob delete succeeded; orphans tracked separately."
    ),
    labelnames=("source_id", "success"),
)

# Defined for v0.4 timeseries wiring; does not increment yet in v0.3.
observation_writes = _LazyCounter(
    name="aslan_observation_writes_total",
    documentation="Observation rows persisted by the v0.4 ObservationWriter.",
    labelnames=("source_id", "kind"),
)

object_storage_orphans = _LazyCounter(
    name="aslan_object_storage_orphans_total",
    documentation=(
        "Object-storage cleanup failures detected by "
        "_log_orphan_cleanup_failed — bytes left in the bucket with "
        "no DB pointer."
    ),
    labelnames=("bucket",),
)

audit_events = _LazyCounter(
    name="aslan_audit_events_total",
    documentation="Rows successfully inserted into audit.events.",
    labelnames=("operation", "actor_kind"),
)


# ── Histograms ───────────────────────────────────────────────────────

# Bucket choice: 5 ms minimum (matches typical local Postgres p50 for
# a single-row UPSERT) up to 10 s (long backfills); covers the
# steady-state and tail without too many cells per labelset.
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

db_query_duration = _LazyHistogram(
    name="aslan_db_query_duration_seconds",
    documentation=(
        "Per-public-method DB-phase wall-clock duration. Wired by the "
        "@traced decorator's timing wrapper."
    ),
    labelnames=("operation",),
    buckets=_DURATION_BUCKETS,
)

object_storage_op_duration = _LazyHistogram(
    name="aslan_object_storage_op_duration_seconds",
    documentation=("Per-S3-op wall-clock duration. op=put|get|delete|head."),
    labelnames=("op",),
    buckets=_DURATION_BUCKETS,
)


# ── Gauges ───────────────────────────────────────────────────────────

advisory_lock_holders = _LazyGauge(
    name="aslan_advisory_lock_holders",
    documentation=(
        "Approximate count of currently-held per-source-ref advisory "
        "locks (debugging tool — incremented on lock acquisition, "
        "decremented on release)."
    ),
)


__all__ = [
    "advisory_lock_holders",
    "audit_events",
    "db_query_duration",
    "entity_creates",
    "entity_merge_required",
    "filing_puts",
    "filing_releases",
    "object_storage_op_duration",
    "object_storage_orphans",
    "observation_writes",
]
