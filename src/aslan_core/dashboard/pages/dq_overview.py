"""/dq/overview — operational-audit roll-up heatmap.

5x4 cell grid (sources x dimensions) per spec §10.2:

  rows:    kap, evds, bist, tefas, mkk
  columns: recency, coverage, validation, spot_check

Each cell carries a state (ok/warn/crit/empty) plus a short text glyph
(lag in seconds, coverage percentage, etc.). Heatmap colours come
from the dashboard.css ``aslan-heat-{state}`` classes.
"""

from __future__ import annotations

from fasthtml.common import H1, Div, P, Table, Td, Th, Tr
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_session_factory
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import (
    DqHeatmapCellVM,
    DqOverviewVM,
    DqStubVM,
)


def _build_dq_stub_body(vm: DqStubVM) -> object:
    """Shared body builder for every M0 /dq/* stub page that has not
    yet been replaced by a real M1 handler.

    Registered against ``DqStubVM`` here so any unmigrated /dq/* page
    can construct a stub VM and render it without each module having
    to repeat the body builder.
    """
    return Div(
        H1(vm.title),
        P(vm.body_message, _id=vm.stub_id),
    )


register_template(DqStubVM, _build_dq_stub_body)


_DIMENSIONS = ("recency", "coverage", "validation", "spot_check")
_DIMENSION_LABELS = {
    "recency": "Recency",
    "coverage": "Coverage",
    "validation": "Validation",
    "spot_check": "Spot-check",
}


def _build_dq_overview_body(vm: DqOverviewVM) -> object:
    """5-row x 4-col heatmap. Each cell is a ``<td>`` whose CSS class
    encodes the state (ok/warn/crit/empty) so dashboard.css can style
    the colours."""
    cell_index: dict[tuple[str, str], DqHeatmapCellVM] = {
        (c.source, c.dimension): c for c in vm.cells
    }
    sources = ("kap", "evds", "bist", "tefas", "mkk")
    header = Tr(Th("Source"), *(Th(_DIMENSION_LABELS[d]) for d in _DIMENSIONS))
    body_rows = []
    for src in sources:
        cells = [Th(src.upper())]
        for dim in _DIMENSIONS:
            cell = cell_index.get((src, dim))
            if cell is None:
                cells.append(Td("—", cls="aslan-heat aslan-heat-empty"))
                continue
            cells.append(
                Td(
                    cell.text,
                    cls=f"aslan-heat aslan-heat-{cell.state.value}",
                    _data_source=cell.source,
                    _data_dimension=cell.dimension,
                )
            )
        body_rows.append(Tr(*cells))
    return Div(
        H1("Data Quality — Overview"),
        Table(header, *body_rows, cls="aslan-heatmap"),
    )


register_template(DqOverviewVM, _build_dq_overview_body)


@app.get("/dq/overview")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_overview(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.dq_overview(session)
    return render(request, vm)
