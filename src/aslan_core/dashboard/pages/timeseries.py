"""Timeseries page — ``/timeseries``."""

from __future__ import annotations

from fasthtml.common import H1, Div, Table, Tbody, Td, Th, Thead, Tr
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_session_factory
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import SeriesRowVM, TimeseriesVM


def _build_timeseries_body(vm: TimeseriesVM) -> object:
    headers = (
        "series_id",
        "code",
        "source",
        "metric",
        "frequency",
        "last_observation_at",
        "obs_24h",
        "has_gap",
    )
    return Div(
        H1("Timeseries"),
        Table(
            Thead(Tr(*[Th(h) for h in headers])),
            Tbody(*[_series_row(r) for r in vm.rows]),
            cls="aslan-table",
        ),
        cls="aslan-timeseries",
    )


def _series_row(row: SeriesRowVM) -> object:
    return Tr(
        Td(str(row.series_id)),
        Td(row.series_code),
        Td(row.source_id),
        Td(row.metric),
        Td(row.frequency),
        Td(row.last_observation_at.isoformat() if row.last_observation_at else "—"),
        Td(str(row.observation_count_last_24h)),
        Td("yes" if row.has_gap else "no"),
    )


register_template(TimeseriesVM, _build_timeseries_body)


@app.get("/timeseries")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def timeseries_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.timeseries_overview(session, limit=100)
    return render(request, vm)
