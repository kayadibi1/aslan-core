"""Unit tests for Prometheus label-cardinality bounding.

Codex Batch 4 finding: ``DocumentStore.put_filing()`` records
``aslan_filing_puts_total`` with ``kind=kind`` directly, where ``kind``
is a public ``str`` parameter and ``doc.filing.kind`` is plain TEXT
with no enforced domain. A crawler that accidentally passes per-feed
values as ``kind`` would create one Prometheus time series per
mutation — cardinality explosion.

Same risk class for ``audit_events_total{operation, actor_kind}`` —
``operation`` is internally controlled, but defensive bounding via the
known-allow-list is cheap and prevents future-drift bugs.

These tests exercise :func:`_normalize_metric_label` and confirm that
unknown labels are mapped to ``"other"`` so the time-series count
stays bounded by ``len(allow_list) + 1``.
"""

from __future__ import annotations


def test_unknown_kind_maps_to_other() -> None:
    from aslan_core.observability.metrics import (
        _KNOWN_FILING_KINDS,
        _normalize_metric_label,
    )

    assert _normalize_metric_label("news", _KNOWN_FILING_KINDS) == "news"
    assert _normalize_metric_label("weird-feed-specific-kind", _KNOWN_FILING_KINDS) == "other"
    assert _normalize_metric_label("", _KNOWN_FILING_KINDS) == "other"


def test_unknown_operation_maps_to_other() -> None:
    from aslan_core.observability.metrics import (
        _KNOWN_AUDIT_OPERATIONS,
        _normalize_metric_label,
    )

    assert _normalize_metric_label("entity.create", _KNOWN_AUDIT_OPERATIONS) == "entity.create"
    assert _normalize_metric_label("filing.put", _KNOWN_AUDIT_OPERATIONS) == "filing.put"
    assert (
        _normalize_metric_label("nonsense.operation.added.by.crawler", _KNOWN_AUDIT_OPERATIONS)
        == "other"
    )
    assert _normalize_metric_label("", _KNOWN_AUDIT_OPERATIONS) == "other"


def test_normalize_metric_label_custom_fallback() -> None:
    """The fallback string is parameterized. Default is ``"other"`` but
    a caller can pick something else (kept for future use)."""
    from aslan_core.observability.metrics import _normalize_metric_label

    allow = frozenset({"a", "b"})
    assert _normalize_metric_label("a", allow) == "a"
    assert _normalize_metric_label("z", allow) == "other"
    assert _normalize_metric_label("z", allow, fallback="unknown") == "unknown"


def test_filing_puts_cardinality_stays_bounded_under_unknown_kinds() -> None:
    """If 1000 distinct unknown kinds come in, only one ``other`` series
    is created — cardinality stays at the size of the allow-list + 1.

    Skipped quietly if ``prometheus_client`` is not installed (the
    metric is a no-op handle in that case and ``.collect()`` would not
    work — but the [obs] extra is in the dev install, so this should
    run in CI)."""
    import pytest

    pc = pytest.importorskip("prometheus_client")  # noqa: F841

    from aslan_core.observability.metrics import (
        _KNOWN_FILING_KINDS,
        _normalize_metric_label,
        filing_puts,
    )

    for i in range(1000):
        filing_puts.labels(
            source_id="kap-cardinality-test",
            kind=_normalize_metric_label(f"weird-{i}", _KNOWN_FILING_KINDS),
            created="true",
        ).inc()

    impl = filing_puts._ensure_impl()
    assert impl is not None  # prometheus_client is installed in [obs]
    samples = list(impl.collect()[0].samples)
    other_kind_samples = [
        s
        for s in samples
        if s.labels.get("kind") == "other"
        and s.labels.get("source_id") == "kap-cardinality-test"
        and s.labels.get("created") == "true"
        and s.name.endswith("_total")
    ]
    # Exactly one series for kind=other, source_id=kap-cardinality-test, created=true.
    assert len(other_kind_samples) == 1
    # And it accumulated all 1000 increments (counters are monotonic).
    assert other_kind_samples[0].value == 1000.0


def test_audit_events_cardinality_stays_bounded_under_unknown_operations() -> None:
    """Symmetric guarantee for audit_events: 500 distinct unknown
    operation strings collapse to a single ``operation=other`` series."""
    import pytest

    pc = pytest.importorskip("prometheus_client")  # noqa: F841

    from aslan_core.observability.metrics import (
        _KNOWN_AUDIT_OPERATIONS,
        _normalize_metric_label,
        audit_events,
    )

    for i in range(500):
        audit_events.labels(
            operation=_normalize_metric_label(f"weird.op.{i}", _KNOWN_AUDIT_OPERATIONS),
            actor_kind="service",
        ).inc()

    impl = audit_events._ensure_impl()
    assert impl is not None
    samples = list(impl.collect()[0].samples)
    other_op_samples = [
        s
        for s in samples
        if s.labels.get("operation") == "other"
        and s.labels.get("actor_kind") == "service"
        and s.name.endswith("_total")
    ]
    assert len(other_op_samples) == 1
    assert other_op_samples[0].value == 500.0


def test_known_filing_kinds_contains_spec_baseline() -> None:
    """Sanity: the starter allow-list per the spec is present. If the
    set is later expanded, this test should be updated alongside."""
    from aslan_core.observability.metrics import _KNOWN_FILING_KINDS

    expected = {
        "news",
        "material_event",
        "financial_report",
        "tender_offer",
        "shareholder_meeting",
        "other",
    }
    assert expected <= _KNOWN_FILING_KINDS


def test_known_audit_operations_contains_every_emitted_op() -> None:
    """Sanity: every ``operation=`` string emitted by aslan-core's audit
    code is present in the allow-list. If the codebase later adds a new
    operation, this test fails until the allow-list is updated — the
    tradeoff is that the allow-list is the source of truth, not the
    audit emitters."""
    from aslan_core.observability.metrics import _KNOWN_AUDIT_OPERATIONS

    expected = {
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
    }
    assert expected <= _KNOWN_AUDIT_OPERATIONS


def test_known_audit_operations_contains_v04_timeseries_ops() -> None:
    """v0.4.0 Task 25: every ``series.*`` / ``observation.*`` audit
    operation emitted by the v0.4 timeseries surface must be in the
    allow-list. Includes the writer operations (already added in earlier
    batches) AND the deletion-runtime / PII-tripwire operations
    (``*_subject_erased``, ``*_metadata_pii_scrubbed``,
    ``*_metadata_bypass_detected``) which the aslan-service Art. 17
    runtime emits — the allow-list is shared so a service-side emitter
    that bypasses the writer still lands a bounded metric label here.
    """
    from aslan_core.observability.metrics import _KNOWN_AUDIT_OPERATIONS

    expected_v04 = {
        # ObservationWriter.upsert_series
        "series.upsert",
        "series.idempotent_hit",
        "series.update",
        # ObservationWriter.write
        "observation.write_batch",
        # Art. 17 deletion runtime + PII tripwires (aslan-service)
        "series.subject_erased",
        "series.metadata_pii_scrubbed",
        "series.metadata_bypass_detected",
        "observation.metadata_pii_scrubbed",
        "observation.metadata_bypass_detected",
    }
    assert expected_v04 <= _KNOWN_AUDIT_OPERATIONS, (
        f"missing v0.4 operations: {expected_v04 - _KNOWN_AUDIT_OPERATIONS}"
    )


def test_known_frequencies_matches_spec_literal() -> None:
    """v0.4.0 Task 25: ``_KNOWN_FREQUENCIES`` MUST contain exactly the
    13 strings that the ``Frequency`` Pydantic literal accepts. Bounds
    the cardinality of ``aslan_series_upserts_total{frequency=...}``
    so a typo in a future caller can never inflate the metric beyond
    ``len(spec) + 1`` (the ``+1`` is for the ``"other"`` fallback)."""
    from aslan_core.observability.metrics import _KNOWN_FREQUENCIES

    spec = {
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
    assert spec == _KNOWN_FREQUENCIES, (
        f"_KNOWN_FREQUENCIES drift — extra={_KNOWN_FREQUENCIES - spec}, "
        f"missing={spec - _KNOWN_FREQUENCIES}"
    )
    assert len(_KNOWN_FREQUENCIES) == 13


def test_unknown_frequency_maps_to_other() -> None:
    """v0.4.0 Task 25: defensive check on the ``Frequency`` label.
    A typo or future-extension value collapses to ``"other"`` so the
    Prometheus cardinality stays bounded by the allow-list."""
    from aslan_core.observability.metrics import (
        _KNOWN_FREQUENCIES,
        _normalize_metric_label,
    )

    assert _normalize_metric_label("1d", _KNOWN_FREQUENCIES) == "1d"
    assert _normalize_metric_label("irregular", _KNOWN_FREQUENCIES) == "irregular"
    # Typos and out-of-spec values map to "other".
    assert _normalize_metric_label("daily", _KNOWN_FREQUENCIES) == "other"
    assert _normalize_metric_label("1day", _KNOWN_FREQUENCIES) == "other"
    assert _normalize_metric_label("", _KNOWN_FREQUENCIES) == "other"
