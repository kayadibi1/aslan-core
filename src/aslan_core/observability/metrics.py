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

Cardinality bounding (codex Batch 4):

  * ``kind`` on ``filing_puts`` is a public ``str`` parameter on
    ``DocumentStore.put_filing``. The ``doc.filing.kind`` column is
    plain TEXT with no enforced domain — a crawler that accidentally
    passes a per-feed value would create one Prometheus time series
    per mutation. To bound this, every call site normalizes the value
    against :data:`_KNOWN_FILING_KINDS` via
    :func:`_normalize_metric_label`; unknown values map to ``"other"``.
  * ``operation`` on ``audit_events`` is internally controlled today,
    but the same defensive normalization against
    :data:`_KNOWN_AUDIT_OPERATIONS` keeps the bound stable if a future
    code path emits a wider value.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)


# ── Allow-lists for cardinality bounding ─────────────────────────────

_KNOWN_FILING_KINDS: frozenset[str] = frozenset(
    {
        "news",
        "material_event",
        "financial_report",
        "tender_offer",
        "shareholder_meeting",
        "other",
    }
)
"""Starter allow-list for ``filing_puts.labels(kind=...)``. Any
``kind`` outside this set collapses to ``"other"`` so the cardinality
of the metric stays bounded by ``len(_KNOWN_FILING_KINDS)``. Expand
deliberately when a new domain value is added to the spec."""

_KNOWN_FREQUENCIES: frozenset[str] = frozenset(
    {
        "tick",
        "1s",
        "1m",
        "5m",
        "15m",
        "30m",
        "1h",
        "1d",
        "1w",
        "1mo",
        "1q",
        "1y",
        "irregular",
    }
)
"""Allow-list of every ``frequency`` string the v0.4 timeseries Pydantic
``Frequency`` literal accepts. Bounds the cardinality of
``aslan_series_upserts_total{frequency=...}``: any value outside this
set collapses to ``"other"`` (the same defensive normalisation pattern
used for ``filing_puts.kind`` and ``audit_events.operation``)."""

_KNOWN_AUDIT_OPERATIONS: frozenset[str] = frozenset(
    {
        "entity.create",
        "entity.idempotent_hit",
        "entity.update",
        "identifier.add",
        "identifier.idempotent_hit",
        "identifier.expire",
        "entity_sector.upsert",
        "entity_sector.idempotent_hit",
        "entity_relationship.link",
        "entity_relationship.idempotent_hit",
        "sector.upsert",
        "sector.idempotent_hit",
        "filing.put",
        "filing.dedup_hit",
        "filing.idempotent_hit",
        "filing.republished_alias_added",
        "filing_body.create",
        "filing_body.update",
        "filing.release",
        "watermark.set",
        "watermark.idempotent_hit",
        "watermark.advance",
        "watermark.force_set",
        "ingestion_run.start",
        "ingestion_run.complete",
        "ingestion_run.set_metadata",
        "ingestion_run.increment_rows",
        # ``series.*`` operations from ObservationWriter.upsert_series
        # (v0.4.0 Tasks 8-13).
        "series.upsert",
        "series.idempotent_hit",
        "series.update",
        # Bulk-write batch event from ObservationWriter.write (Task 16).
        # ONE event per write() call carrying bounded forensic
        # metadata; per-key detail lives in audit.observation_batch_keys.
        "observation.write_batch",
        # v0.4.0 Task 25 — Art. 17 deletion runtime + PII tripwires.
        # Emitted by the aslan-service deletion runtime (NOT by aslan-core
        # itself; aslan-core only enforces upsert-time PII rejection).
        # Listed here so a service-side emitter that lands an audit row
        # under one of these operations gets a bounded Prometheus label
        # instead of inflating cardinality.
        "series.subject_erased",
        "series.metadata_pii_scrubbed",
        "series.metadata_bypass_detected",
        "observation.metadata_pii_scrubbed",
        "observation.metadata_bypass_detected",
    }
)
"""Allow-list of every ``operation=`` string emitted by aslan-core's
own audit recorder call sites. Defensive: ``operation`` is internally
controlled, but a new emitter that forgets to update this set will
collapse to ``"other"`` rather than create an unbounded metric. Update
this set whenever a new ``operation`` string is added to a public
mutation path; the corresponding allow-list test in
``tests/unit/test_metrics_cardinality.py`` enforces parity."""


def _normalize_metric_label(
    value: str,
    allow_list: frozenset[str],
    fallback: str = "other",
) -> str:
    """Bound the cardinality of a Prometheus label.

    If ``value`` is a known member of ``allow_list``, return it
    unchanged. Otherwise, return ``fallback`` (default ``"other"``).
    Used at every Prometheus label call site where the input string
    is not provably bounded by a CHECK constraint or a closed enum.

    The contract is intentionally narrow — this function does not
    validate the database column, only the label that ends up on the
    metric. Mismatches between DB and metric are acceptable: the DB
    keeps the raw ``kind`` for analytics, and the metric collapses
    unknowns to a single ``"other"`` time series for monitoring.

    :param value: Caller-supplied label value, possibly unbounded.
    :param allow_list: Closed set of permitted values.
    :param fallback: String to return when ``value`` is not in
        ``allow_list``. Defaults to ``"other"`` so the bound is at
        most ``len(allow_list) + 1`` (the ``+ 1`` is for ``fallback``
        itself when it is not already in the set).
    :returns: ``value`` if known, else ``fallback``.
    """
    if value in allow_list:
        return value
    return fallback


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

series_upserts = _LazyCounter(
    name="aslan_series_upserts_total",
    documentation=(
        "Total ObservationWriter.upsert_series calls (fresh + idempotent + "
        "field-change paths combined)."
    ),
    labelnames=("source_id", "frequency"),
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

# v0.4 ObservationWriter.write — per-batch sample size (used by
# operators for capacity planning + p99 batch-size alerts). No labels:
# the histogram is a global signal; per-source breakdown lives on
# ``observation_writes``.
observation_write_batch_size = _LazyHistogram(
    name="aslan_observation_write_batch_size",
    documentation="Per-write batch size (number of ObservationIn rows attempted).",
    buckets=(1, 10, 100, 500, 1_000, 5_000, 10_000, 50_000),
)

# End-to-end wall-clock for ObservationWriter.write. Wired in Task 19;
# declared here so the metric is registered before the first call.
observation_write_duration = _LazyHistogram(
    name="aslan_observation_write_duration_seconds",
    documentation="ObservationWriter.write end-to-end duration.",
    labelnames=("source_id",),
    buckets=(0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0),
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
    "observation_write_batch_size",
    "observation_write_duration",
    "observation_writes",
    "series_upserts",
]

# The cardinality-bounding helper and allow-lists are intentionally
# module-private (prefixed with ``_``) — call sites import them
# directly. They are not part of the public re-export surface.
