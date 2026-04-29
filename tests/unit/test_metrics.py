"""Unit tests for ``aslan_core.observability.metrics``.

The metrics module wraps ``prometheus_client`` behind a lazy-import
shim so the base install (without ``aslan-core[obs]``) can import
the module and read counter / histogram handles without an
``ImportError``. The handles raise only on ``.inc()`` / ``.observe()``
when prometheus_client is missing.

Tests here verify:
  * Module imports cleanly without prometheus_client installed.
  * Counter / histogram handle attributes exist and are well-formed.
  * Calling ``.labels(...).inc()`` is either a real increment (when
    prometheus_client is installed) or a no-op (when missing) — never
    raises an attribute error.
  * Label cardinality is bounded (no entity_id-style unbounded labels).
"""

from __future__ import annotations

from typing import Any


def test_metrics_module_imports_without_obs_extra() -> None:
    """``from aslan_core.observability import metrics`` must work
    with or without prometheus_client installed."""
    from aslan_core.observability import metrics

    # Counter handles exist as attributes.
    assert hasattr(metrics, "entity_creates")
    assert hasattr(metrics, "entity_merge_required")
    assert hasattr(metrics, "filing_puts")
    assert hasattr(metrics, "filing_releases")
    assert hasattr(metrics, "observation_writes")
    assert hasattr(metrics, "object_storage_orphans")
    assert hasattr(metrics, "audit_events")
    # Histogram handles exist.
    assert hasattr(metrics, "db_query_duration")
    assert hasattr(metrics, "object_storage_op_duration")
    # Gauge handle exists.
    assert hasattr(metrics, "advisory_lock_holders")


def test_counter_inc_is_safe_without_prometheus_client() -> None:
    """The counter's ``.labels(...).inc()`` chain must not raise even
    when prometheus_client is not importable. In that case it is a
    no-op."""
    from aslan_core.observability import metrics

    metrics.entity_creates.labels(source_id="kap").inc()
    metrics.filing_puts.labels(source_id="kap", kind="news", created="true").inc()
    metrics.filing_releases.labels(source_id="kap", success="true").inc()
    metrics.audit_events.labels(operation="entity.create", actor_kind="user").inc()
    metrics.object_storage_orphans.labels(bucket="aslan-filings").inc()


def test_histogram_observe_is_safe_without_prometheus_client() -> None:
    """The histogram's ``.labels(...).observe()`` chain is also no-op-
    safe when prometheus_client is missing."""
    from aslan_core.observability import metrics

    metrics.db_query_duration.labels(operation="put_filing").observe(0.123)
    metrics.object_storage_op_duration.labels(op="put").observe(0.045)


def test_gauge_inc_dec_is_safe_without_prometheus_client() -> None:
    from aslan_core.observability import metrics

    metrics.advisory_lock_holders.inc()
    metrics.advisory_lock_holders.dec()


def test_counter_label_names_bounded() -> None:
    """Sanity: label names are the bounded ones documented in the spec
    (no unbounded entity_id / request_id labels)."""
    from aslan_core.observability import metrics

    # Each counter handle exposes its labelnames via _labelnames so
    # tests can assert the exact set without reaching into the prom
    # client's private API.
    expected: dict[str, tuple[str, ...]] = {
        "entity_creates": ("source_id",),
        "entity_merge_required": ("source_id",),
        "filing_puts": ("source_id", "kind", "created"),
        "filing_releases": ("source_id", "success"),
        "observation_writes": ("source_id", "kind"),
        "object_storage_orphans": ("bucket",),
        "audit_events": ("operation", "actor_kind"),
        "db_query_duration": ("operation",),
        "object_storage_op_duration": ("op",),
    }
    for name, want in expected.items():
        handle: Any = getattr(metrics, name)
        assert tuple(handle.labelnames) == want, (
            f"metric {name!r} labelnames diverge: got {handle.labelnames}, want {want}"
        )
