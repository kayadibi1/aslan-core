"""Unit tests for aslan_core.dq.types."""

from __future__ import annotations

import pytest

from aslan_core.dq.types import Severity, SyncRunStatus, ValidationFailure


def test_sync_run_status_values() -> None:
    assert {s.value for s in SyncRunStatus} == {
        "running",
        "ok",
        "partial",
        "failed",
    }


def test_severity_values() -> None:
    assert {s.value for s in Severity} == {"info", "warn", "error", "critical"}


def test_validation_failure_is_immutable() -> None:
    vf = ValidationFailure(
        source="kap",
        rule_name="required_field_present",
        severity=Severity.WARN,
        record_table="ts.canonical_financial",
        record_pk={"entity_id": 1, "as_of": "2026-01-01T00:00:00Z"},
        detail={"missing_field": "revenue"},
    )
    with pytest.raises(AttributeError):
        vf.source = "evds"  # type: ignore[misc]


def test_validation_failure_required_fields() -> None:
    with pytest.raises(TypeError):
        ValidationFailure()  # type: ignore[call-arg]
