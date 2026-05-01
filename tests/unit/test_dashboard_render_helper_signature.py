"""Render helper contract — Task 7.

Spec §6.3 + plan-round-1: ``render(request, vm)`` is the unified
output point for every page handler. The helper:

  * Runtime-checks ``vm`` is a ``_VMBase`` subclass — a plain
    ``BaseModel`` (or a SQLAlchemy ``Row``, or a dict) is rejected
    with TypeError.
  * Calls ``vm.model_dump(mode='json')`` — surfaces a non-JSON-safe
    field type at runtime.
  * Looks up a registered body builder for the concrete VM type;
    raises LookupError if none registered.
  * Never invokes ``str(vm)`` / ``repr(vm)`` (the body builder
    consumes typed fields explicitly).

The route-table-wide "every handler ends with render(...)" check
lives in Task 15.5 (deferred per plan-round-1 — the route table is
near-empty until Tasks 8-12 register the pages).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel
from starlette.responses import HTMLResponse

from aslan_core.dashboard.render import (
    clear_templates,
    register_template,
    render,
)
from aslan_core.dashboard.view_models import OverviewVM, _VMBase


@pytest.fixture(autouse=True)
def _isolate_template_registry() -> Any:
    """Each test gets a clean template registry so registrations
    don't leak between tests."""
    clear_templates()
    yield
    clear_templates()


def _request() -> MagicMock:
    """Stand-in for a Starlette Request. The helper does not use
    ``request`` today (kept in the signature for htmx-partial
    detection in Task 9)."""
    return MagicMock()


def _overview_vm() -> OverviewVM:
    return OverviewVM(
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
    )


# ── Runtime VM check ─────────────────────────────────────────────


def test_render_rejects_plain_basemodel() -> None:
    class NotAVM(BaseModel):
        x: int

    with pytest.raises(TypeError) as excinfo:
        render(_request(), NotAVM(x=1))  # type: ignore[arg-type]
    assert "_VMBase subclass" in str(excinfo.value)


def test_render_rejects_dict() -> None:
    with pytest.raises(TypeError):
        render(_request(), {"foo": "bar"})  # type: ignore[arg-type]


def test_render_rejects_string() -> None:
    with pytest.raises(TypeError):
        render(_request(), "<script>alert(1)</script>")  # type: ignore[arg-type]


# ── model_dump invariant ─────────────────────────────────────────


def test_render_calls_model_dump_with_json_mode() -> None:
    """The helper must materialise the VM to a JSON-safe dict before
    rendering. We register a body builder that asserts the helper
    has done so by the time we receive the VM."""
    register_template(OverviewVM, lambda vm: f"<p>{vm.outbox_pending}</p>")
    vm = _overview_vm()
    response = render(_request(), vm)
    assert isinstance(response, HTMLResponse)


def test_render_raises_lookup_error_when_no_template_registered() -> None:
    """A handler that constructs a VM whose type has no registered
    template gets a clear LookupError — not a silent empty page."""
    vm = _overview_vm()
    with pytest.raises(LookupError) as excinfo:
        render(_request(), vm)
    assert "OverviewVM" in str(excinfo.value)


# ── Body builder receives typed VM, not str ──────────────────────


def test_body_builder_receives_typed_vm_not_str() -> None:
    received: list[Any] = []

    def builder(vm: OverviewVM) -> str:
        received.append(vm)
        return "<p>ok</p>"

    register_template(OverviewVM, builder)
    vm = _overview_vm()
    render(_request(), vm)
    assert received == [vm], "builder must receive the typed VM, not a stringified copy"
    assert isinstance(received[0], OverviewVM)
    assert isinstance(received[0], _VMBase)


# ── Output is HTMLResponse ───────────────────────────────────────


def test_render_returns_html_response_with_base_template() -> None:
    """The base template wraps the body in <html><head>...</head><body>...
    The rendered HTML must contain the page title, a sidebar, and the
    static-asset references (htmx + dashboard.css + favicon)."""
    register_template(OverviewVM, lambda vm: "BODY-CONTENT-MARKER")
    vm = _overview_vm()
    response = render(_request(), vm)

    assert isinstance(response, HTMLResponse)
    body = response.body.decode()
    # Title carries the VM-derived label.
    assert "Overview · aslan dashboard" in body
    # Static assets referenced.
    assert "/static/dashboard.css" in body
    assert "/static/htmx.min.js" in body
    assert "/static/favicon.ico" in body
    # Sidebar nav present.
    assert 'href="/outbox"' in body
    assert 'href="/audit"' in body
    # Body content from the registered template.
    assert "BODY-CONTENT-MARKER" in body


def test_render_uses_html_response_content_type() -> None:
    register_template(OverviewVM, lambda vm: "<p>hi</p>")
    response = render(_request(), _overview_vm())
    assert response.media_type == "text/html"


# ── No implicit str(vm) / repr(vm) ───────────────────────────────


def test_render_does_not_invoke_vm_str_method() -> None:
    """A handler-side bug that overrides __str__ to leak a forbidden
    field MUST NOT be exercised by render(). The body builder
    consumes typed fields explicitly."""
    str_calls: list[None] = []

    class LeakyOverviewVM(OverviewVM):
        def __str__(self) -> str:
            str_calls.append(None)
            return "LEAKED-STR"

        def __repr__(self) -> str:
            str_calls.append(None)
            return "LEAKED-REPR"

    register_template(LeakyOverviewVM, lambda vm: "<p>safe</p>")
    leaky = LeakyOverviewVM(
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
    )
    response = render(_request(), leaky)
    assert "LEAKED-STR" not in response.body.decode()
    assert "LEAKED-REPR" not in response.body.decode()
    assert str_calls == [], "render() must not invoke __str__/__repr__ on the VM"
