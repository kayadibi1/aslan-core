"""Ingestion page — ``/ingestion``."""

from __future__ import annotations

from fasthtml.common import H1, Div, Table, Tbody, Td, Th, Thead, Tr
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_session_factory
from aslan_core.dashboard.formatters import format_age_s
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import IngestionRowVM, IngestionVM


def _build_ingestion_body(vm: IngestionVM) -> object:
    headers = (
        "run_id",
        "source",
        "job",
        "status",
        "started_at",
        "duration",
        "events",
        "last_error_kind",
    )
    return Div(
        H1("Ingestion"),
        Table(
            Thead(Tr(*[Th(h) for h in headers])),
            Tbody(*[_ingestion_row(r) for r in vm.rows]),
            cls="aslan-table",
        ),
        cls="aslan-ingestion",
    )


def _ingestion_row(row: IngestionRowVM) -> object:
    duration = format_age_s(row.duration_s) if row.duration_s is not None else "—"
    events = str(row.event_count) if row.event_count is not None else "—"
    return Tr(
        Td(str(row.ingestion_run_id)),
        Td(row.source_id),
        Td(row.job_name),
        Td(row.status),
        Td(row.started_at.isoformat()),
        Td(duration),
        Td(events),
        Td(row.last_error_kind or "—"),
    )


register_template(IngestionVM, _build_ingestion_body)


@app.get("/ingestion")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def ingestion_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.ingestion_recent(session, limit=100)
    return render(request, vm)
