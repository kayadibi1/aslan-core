"""/dq/coverage — tabular view of latest coverage_snapshot rows.

One row per (source, dimension) — the most-recent snapshot per pair.
"""

from __future__ import annotations

from fasthtml.common import H1, Div, Table, Td, Th, Tr
from starlette.requests import Request
from starlette.responses import HTMLResponse

# Side-effect import: shared DqStubVM template lives in dq_overview.
import aslan_core.dashboard.pages.dq_overview  # noqa: F401
from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_session_factory
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import DqCoverageVM


def _fmt_int(n: int | None) -> str:
    return "—" if n is None else f"{n:,}"


def _fmt_pct(p: float | None) -> str:
    return "—" if p is None else f"{p:.2f}%"


def _build_dq_coverage_body(vm: DqCoverageVM) -> object:
    header = Tr(
        Th("Source"),
        Th("Dimension"),
        Th("Expected"),
        Th("Actual"),
        Th("Coverage"),
        Th("Target"),
        Th("State"),
        Th("Observed at"),
    )
    body_rows = []
    if not vm.rows:
        body_rows.append(Tr(Td("No coverage snapshots recorded", colspan="8")))
    for row in vm.rows:
        body_rows.append(
            Tr(
                Th(row.source.upper()),
                Td(row.dimension),
                Td(_fmt_int(row.expected_count)),
                Td(_fmt_int(row.actual_count)),
                Td(_fmt_pct(row.coverage_pct)),
                Td(_fmt_pct(row.target_pct)),
                Td(
                    row.state.value,
                    cls=f"aslan-heat aslan-heat-{row.state.value}",
                ),
                Td(row.observed_at.isoformat()),
            )
        )
    return Div(H1("Data Quality — Coverage"), Table(header, *body_rows, cls="aslan-coverage-table"))


register_template(DqCoverageVM, _build_dq_coverage_body)


@app.get("/dq/coverage")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_coverage(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.dq_coverage(session)
    return render(request, vm)
