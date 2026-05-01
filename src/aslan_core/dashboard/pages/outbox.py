"""Outbox page — ``/outbox``.

Renders a table of recent outbox rows (most recent first). Each row
projects only the columns the dashboard role's GRANT permits;
``payload`` and raw ``last_error`` are forbidden by migration 0020,
``client_ip`` / ``user_agent`` by migration 0022.
"""

from __future__ import annotations

from fasthtml.common import H1, H2, Div, P, Table, Tbody, Td, Th, Thead, Tr
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_session_factory
from aslan_core.dashboard.formatters import format_id_prefix
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import OutboxRowVM, OutboxVM


def _build_outbox_body(vm: OutboxVM) -> object:
    headers = (
        "outbox_id",
        "stream",
        "event_id",
        "source",
        "created_at",
        "published_at",
        "attempts",
        "last_attempt",
        "last_error_kind",
    )
    return Div(
        H1("Outbox"),
        P(f"Pending total: {vm.pending_total}"),
        H2("Recent rows"),
        Table(
            Thead(Tr(*[Th(h) for h in headers])),
            Tbody(*[_outbox_row(r) for r in vm.rows]),
            cls="aslan-table",
        ),
        cls="aslan-outbox",
    )


def _outbox_row(row: OutboxRowVM) -> object:
    return Tr(
        Td(str(row.outbox_id)),
        Td(row.stream_name),
        Td(format_id_prefix(str(row.event_id))),
        Td(row.source_id),
        Td(row.created_at.isoformat()),
        Td(row.published_at.isoformat() if row.published_at else "—"),
        Td(str(row.publish_attempts)),
        Td(row.last_attempt_at.isoformat() if row.last_attempt_at else "—"),
        Td(row.last_error_kind or "—"),
    )


register_template(OutboxVM, _build_outbox_body)


@app.get("/outbox")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def outbox_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.outbox_recent(session, limit=50, offset=0)
    return render(request, vm)
