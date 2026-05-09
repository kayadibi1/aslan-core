"""/dq/bloomberg — Bloomberg-comparison surface (M4).

Three views on this page module:

  * ``GET /dq/bloomberg`` — overview. Shows the latest run's
    wins/ties/loses summary, a 60-cell grid (5 entities x 12 fields),
    a per-NULL-cell entry form, and a History block with click-through
    to past closed runs.

  * ``GET /dq/bloomberg/runs/{run_id}`` — per-run grid (drill-down
    from the History block).

  * ``POST /dq/bloomberg/cells/{cell_id}`` — manual entry submit.
    Persists ``bloomberg_value`` for one cell + 303-redirects back to
    the overview.

Privilege boundary mirrors /dq/spot-check (M2): the dashboard role
itself is SELECT-only on ``audit.bloomberg_comparison_*`` (migration
0060). The POST handler runs the dq.bloomberg helper under a
write-capable session — production wires sidar's authenticated
session against the ``audit_admin`` role, the only role with
column-level UPDATE on ``bloomberg_value``.
"""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from fasthtml.common import (
    H1,
    H2,
    H3,
    A,
    Button,
    Div,
    Form,
    Input,
    P,
    Span,
    Table,
    Td,
    Th,
    Tr,
)
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

# Side-effect import: shared DqStubVM template lives in dq_overview.
import aslan_core.dashboard.pages.dq_overview  # noqa: F401
from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_session_factory
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import (
    DqBloombergCellRowVM,
    DqBloombergOverviewVM,
    DqBloombergRunDetailVM,
    DqBloombergRunSummaryVM,
)
from aslan_core.dq import bloomberg

# ── Input validation ─────────────────────────────────────────────


# Bloomberg values are short strings (numeric, dates, classifiers).
# 256 chars covers every realistic entry; reject overlong input as
# defence against form-paste DoS.
_MAX_BLOOMBERG_VALUE_LEN = 256
_MAX_LABELLER_LEN = 64
_LABELLER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


# ── Body builders ────────────────────────────────────────────────


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.isoformat()


def _summary_block(summary: DqBloombergRunSummaryVM) -> object:
    closed = "open" if summary.closed_at is None else f"closed {_fmt_dt(summary.closed_at)}"
    return Div(
        P(
            f"Quarter {summary.quarter} · {closed}",
            _id="dq-bloomberg-quarter",
        ),
        P(
            f"Aslan: {summary.wins} wins / {summary.ties} ties / {summary.loses} loses "
            f"across {summary.total_cells} cells "
            f"(NULL bloomberg cells: {summary.null_bloomberg_cells}).",
            _id="dq-bloomberg-summary",
        ),
        cls="aslan-bloomberg-summary",
    )


def _verdict_badge(advantage: str | None) -> str:
    if advantage is None:
        return "—"
    return {"wins": "Aslan", "ties": "tie", "loses": "Bloomberg"}.get(advantage, advantage)


def _cells_grid(cells: list[DqBloombergCellRowVM], *, run_id: UUID, run_open: bool) -> object:
    """Render the 60-cell grid grouped by entity. ``run_open`` controls
    whether the per-NULL-cell entry form is rendered (closed runs are
    read-only)."""
    if not cells:
        return P("No cells in this run yet.", _id="dq-bloomberg-no-cells")
    by_entity: dict[str, list[DqBloombergCellRowVM]] = {}
    for c in cells:
        by_entity.setdefault(c.entity_ticker, []).append(c)

    sections: list[object] = []
    for entity, entity_cells in sorted(by_entity.items()):
        rows: list[object] = [
            Tr(
                Th("Field"),
                Th("Aslan"),
                Th("Bloomberg"),
                Th("Variance %"),
                Th("Verdict"),
                Th(""),
            )
        ]
        for c in entity_cells:
            variance_text = "—" if c.variance_pct is None else f"{c.variance_pct:.4f}"
            if run_open and c.bloomberg_value is None:
                action: object = Form(
                    Input(
                        type="text",
                        name="bloomberg_value",
                        placeholder="value",
                        maxlength=str(_MAX_BLOOMBERG_VALUE_LEN),
                        required=True,
                    ),
                    Input(
                        type="text",
                        name="entered_by",
                        placeholder="labeller",
                        maxlength=str(_MAX_LABELLER_LEN),
                        required=True,
                    ),
                    Button("Save", type="submit"),
                    action=f"/dq/bloomberg/cells/{c.cell_id}",
                    method="post",
                    cls="aslan-bloomberg-cell-form",
                )
            else:
                action = Span("—")
            rows.append(
                Tr(
                    Td(c.field),
                    Td(c.aslan_value or "—"),
                    Td(c.bloomberg_value or "—"),
                    Td(variance_text),
                    Td(_verdict_badge(c.aslan_advantage)),
                    Td(action),
                )
            )
        sections.append(
            Div(
                H3(entity),
                Table(*rows, cls="aslan-bloomberg-grid"),
                cls="aslan-bloomberg-entity",
            )
        )
    _ = run_id  # rendered into form actions per cell
    return Div(*sections, cls="aslan-bloomberg-cells")


def _history_block(closed_runs: list[DqBloombergRunSummaryVM]) -> object:
    if not closed_runs:
        return Div(
            H2("History"),
            P("No prior closed runs.", _id="dq-bloomberg-no-history"),
        )
    rows: list[object] = [
        Tr(
            Th("Quarter"),
            Th("Closed at"),
            Th("Wins"),
            Th("Ties"),
            Th("Loses"),
            Th(""),
        )
    ]
    for r in closed_runs:
        rows.append(
            Tr(
                Td(r.quarter),
                Td(_fmt_dt(r.closed_at)),
                Td(str(r.wins)),
                Td(str(r.ties)),
                Td(str(r.loses)),
                Td(A("View", href=f"/dq/bloomberg/runs/{r.run_id}")),
            )
        )
    return Div(
        H2("History"),
        Table(*rows, cls="aslan-bloomberg-history"),
    )


def _build_bloomberg_overview_body(vm: DqBloombergOverviewVM) -> object:
    if vm.latest_run is None:
        return Div(
            H1("Data Quality — Bloomberg Comparison"),
            P(
                "No Bloomberg-comparison run has been opened yet. "
                "Run `aslan-core audit bloomberg-open-quarter` to start one.",
                _id="dq-bloomberg-empty",
            ),
        )
    return Div(
        H1("Data Quality — Bloomberg Comparison"),
        _summary_block(vm.latest_run),
        H2("Cells"),
        _cells_grid(
            vm.cells,
            run_id=vm.latest_run.run_id,
            run_open=vm.latest_run.closed_at is None,
        ),
        _history_block(vm.closed_runs),
    )


def _build_bloomberg_run_detail_body(vm: DqBloombergRunDetailVM) -> object:
    return Div(
        H1(f"Bloomberg Comparison — {vm.run.quarter}"),
        _summary_block(vm.run),
        H2("Cells"),
        # Closed runs are read-only — never render the entry form.
        _cells_grid(
            vm.cells,
            run_id=vm.run.run_id,
            run_open=vm.run.closed_at is None,
        ),
        P(
            A("Back to overview", href="/dq/bloomberg"),
            _id="dq-bloomberg-back",
        ),
    )


register_template(DqBloombergOverviewVM, _build_bloomberg_overview_body)
register_template(DqBloombergRunDetailVM, _build_bloomberg_run_detail_body)


# ── Routes ───────────────────────────────────────────────────────


@app.get("/dq/bloomberg")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_bloomberg(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.dq_bloomberg_overview(session)
    return render(request, vm)


@app.get("/dq/bloomberg/runs/{run_id}")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_bloomberg_run(request: Request, run_id: str) -> Response:
    try:
        rid = UUID(run_id)
    except ValueError:
        return HTMLResponse("<h1>Bad run id</h1>", status_code=400)
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.dq_bloomberg_run_detail(session, run_id=rid)
    if vm is None:
        return HTMLResponse("<h1>Run not found</h1>", status_code=404)
    return render(request, vm)


@app.post("/dq/bloomberg/cells/{cell_id}")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_bloomberg_cell_submit(request: Request, cell_id: str) -> Response:
    """Manual entry POST for one Bloomberg cell.

    Validates the form (bloomberg_value bounded length; entered_by
    matches the labeller-name regex), calls
    ``dq.bloomberg.record_bloomberg_value``, and 303-redirects back to
    /dq/bloomberg. Bad input → 400 with a short error string; unknown
    cell_id → 404.
    """
    try:
        cid = UUID(cell_id)
    except ValueError:
        return HTMLResponse("<h1>Bad cell id</h1>", status_code=400)

    form = await request.form()

    def _form_str(name: str) -> str | None:
        value = form.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        return value

    bloomberg_value_raw = _form_str("bloomberg_value")
    entered_by = (_form_str("entered_by") or "").strip()

    if bloomberg_value_raw is None:
        return HTMLResponse(
            "<h1>Bad input</h1><p>bloomberg_value required</p>",
            status_code=400,
        )
    bloomberg_value = bloomberg_value_raw.strip()
    if not bloomberg_value:
        return HTMLResponse(
            "<h1>Bad input</h1><p>bloomberg_value must not be blank</p>",
            status_code=400,
        )
    if len(bloomberg_value) > _MAX_BLOOMBERG_VALUE_LEN:
        return HTMLResponse(
            f"<h1>Bad input</h1><p>bloomberg_value too long (>{_MAX_BLOOMBERG_VALUE_LEN})</p>",
            status_code=400,
        )
    if not entered_by or not _LABELLER_RE.match(entered_by):
        return HTMLResponse(
            "<h1>Bad input</h1><p>entered_by required, max 64 chars, alphanumeric + . _ @ -</p>",
            status_code=400,
        )

    factory = get_session_factory()
    async with factory() as session:
        try:
            await bloomberg.record_bloomberg_value(
                session=session,
                cell_id=cid,
                bloomberg_value=bloomberg_value,
                entered_by=entered_by,
            )
        except LookupError:
            return HTMLResponse("<h1>Cell not found</h1>", status_code=404)
        await session.commit()
    return RedirectResponse(url="/dq/bloomberg", status_code=303)
