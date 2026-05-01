"""Structural floor: every dashboard view model rejects unknown keys.

Spec §6.3 + §8.1: ``model_config = ConfigDict(extra='forbid', frozen=True)``
is the in-process defense-in-depth that catches a future query selecting
a forbidden column whose name doesn't match the VM's allowlist. The DB
column-allowlist GRANT is the load-bearing GDPR guarantee; this is the
diff-reviewer's signal.

Constructing any VM with an unexpected keyword raises Pydantic
``ValidationError``. Frozen=True makes mutation raise after construction
— a render-helper bug that tries to swap a redacted value back in
fails loudly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_overview_vm_rejects_extra_fields() -> None:
    from aslan_core.dashboard.view_models import OverviewVM

    with pytest.raises(ValidationError):
        OverviewVM(
            outbox_pending=0,
            outbox_oldest_age_s=0.0,
            outbox_drained_last_15m=0,
            streams_total_xlen=0,
            deadletter_total=0,
            deadletter_last_24h=0,
            ingestion_runs_last_24h=0,
            audit_events_per_min_last_60m=0.0,
            redaction_registry_size=0,
            redaction_last_at=None,
            unexpected_field="LEAK",  # type: ignore[call-arg]
        )


def test_outbox_row_vm_rejects_extra_fields() -> None:
    from uuid import uuid4

    from aslan_core.dashboard.view_models import OutboxRowVM

    base_kwargs = {
        "outbox_id": 1,
        "stream_name": "kap",
        "event_id": uuid4(),
        "schema_version": 1,
        "source_id": "kap",
        "created_at": __import__("datetime").datetime.now(__import__("datetime").UTC),
        "published_at": None,
        "publish_attempts": 0,
        "last_attempt_at": None,
        "last_error_kind": None,
        "payload_size_bytes": 0,
    }
    OutboxRowVM(**base_kwargs)  # baseline: valid construction succeeds
    with pytest.raises(ValidationError):
        OutboxRowVM(**base_kwargs, payload="LEAK")  # type: ignore[call-arg]


def test_audit_row_vm_rejects_metadata_field() -> None:
    """Specific guard: the audit page renders ``metadata_key_count``
    (an int from the SECURITY DEFINER helper), never the raw metadata.
    A future refactor that tries to pass ``metadata=...`` MUST fail."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from aslan_core.dashboard.view_models import AuditRowVM

    base_kwargs = {
        "event_id": uuid4(),
        "occurred_at": datetime.now(UTC),
        "actor_id": "user:alice",
        "actor_kind": "user",
        "operation": "insert",
        "target_schema": "streams",
        "target_table": "outbox",
        "client_ip_truncated": "10.0.0.0/24",
        "metadata_key_count": 0,
    }
    AuditRowVM(**base_kwargs)
    with pytest.raises(ValidationError):
        AuditRowVM(**base_kwargs, metadata={"k": "v"})  # type: ignore[call-arg]


def test_vm_is_frozen_after_construction() -> None:
    """``frozen=True`` blocks attribute assignment — a render helper
    that tries to replace a redacted value back in raises ValidationError."""
    from aslan_core.dashboard.view_models import NotFoundVM

    vm = NotFoundVM()
    with pytest.raises(ValidationError):
        vm.title = "Server error"  # type: ignore[assignment,misc]
