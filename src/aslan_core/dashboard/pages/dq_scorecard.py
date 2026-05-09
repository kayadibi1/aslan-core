"""/dq/scorecard — weekly DQ scorecard (M6).

Spec §10.8 + §5.10. Three sections:

  * **Top** — current-week metrics: a 10-row table with metric, target,
    actual, status, and notes. Status cells are colour-coded
    (green/yellow/red) to mirror the email digest.

  * **Middle** — history block. One row per past week in
    ``audit.scorecard_snapshot`` with the pass/warn/fail counts.

  * **Bottom** — buttons:
    * ``Email preview`` — renders the latest scorecard's email body
      verbatim (the body the cron handed to the email sink) inline at
      ``/dq/scorecard/email``.
    * ``Export HTML`` — serves the rendered HTML body for download at
      ``/dq/scorecard/export``.

PDF export is deferred to M6.1 (see ``dq.scorecard.render_html``
TODO). The "Export HTML" download is the supported v1 surface; the
spec's "PDF export" requirement is documented as deferred in the
handoff doc.

Privilege boundary: dashboard role has SELECT-only on
``audit.scorecard_snapshot`` (migration 0062). The page is read-only;
the cron writes new weeks via ``aslan-core audit scorecard``.
"""

from __future__ import annotations

from datetime import datetime

from fasthtml.common import (
    H1,
    H2,
    H3,
    A,
    Div,
    P,
    Table,
    Td,
    Th,
    Tr,
)
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

# Side-effect import: shared DqStubVM template lives in dq_overview.
import aslan_core.dashboard.pages.dq_overview  # noqa: F401
from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_session_factory
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import (
    DqScorecardRowVM,
    DqScorecardVM,
    DqScorecardWeekSummaryVM,
)
from aslan_core.dq import scorecard as scorecard_module

_STATUS_CSS_CLASS: dict[str, str] = {
    "pass": "aslan-scorecard-pass",
    "warn": "aslan-scorecard-warn",
    "fail": "aslan-scorecard-fail",
}


def _fmt_dt(dt: datetime) -> str:
    return dt.isoformat()


def _build_metrics_table(rows: list[DqScorecardRowVM]) -> object:
    header = Tr(
        Th("Metric"),
        Th("Target"),
        Th("Actual"),
        Th("Status"),
        Th("Notes"),
    )
    body: list[object] = []
    if not rows:
        body.append(
            Tr(
                Td(
                    "No scorecard rows yet. Run "
                    "`aslan-core audit scorecard` to populate.",
                    colspan="5",
                )
            )
        )
    for r in rows:
        cls = _STATUS_CSS_CLASS.get(r.status, "")
        body.append(
            Tr(
                Th(r.metric_name),
                Td(r.target),
                Td(r.actual),
                Td(r.status.upper(), cls=cls),
                Td(r.notes or "—"),
            )
        )
    return Table(header, *body, cls="aslan-scorecard-metrics")


def _build_history_table(history: list[DqScorecardWeekSummaryVM]) -> object:
    header = Tr(
        Th("Week start (Mon)"),
        Th("Pass"),
        Th("Warn"),
        Th("Fail"),
        Th("Total"),
        Th("Recorded at"),
    )
    body: list[object] = []
    if not history:
        body.append(Tr(Td("No history yet.", colspan="6")))
    for h in history:
        body.append(
            Tr(
                Th(h.week_start.date().isoformat()),
                Td(str(h.pass_count)),
                Td(str(h.warn_count)),
                Td(str(h.fail_count)),
                Td(str(h.total_count)),
                Td(_fmt_dt(h.recorded_at)),
            )
        )
    return Table(header, *body, cls="aslan-scorecard-history")


def _build_dq_scorecard_body(vm: DqScorecardVM) -> object:
    """Three-section page body for GET /dq/scorecard."""
    summary = (
        f"{vm.pass_pct:.0f}% pass — {vm.pass_count} pass / "
        f"{vm.warn_count} warn / {vm.fail_count} fail "
        f"across {len(vm.rows)} metric(s)."
    )
    week_section = Div(
        H2(f"Current week — {vm.current_week_start.date().isoformat()}"),
        H3(summary, _id="dq-scorecard-summary"),
        _build_metrics_table(vm.rows),
        _id="dq-scorecard-current-week",
    )
    history_section = Div(
        H2("History"),
        P(
            f"{len(vm.history)} prior week(s).",
            _id="dq-scorecard-history-summary",
        ),
        _build_history_table(vm.history),
        _id="dq-scorecard-history",
    )
    actions_section = Div(
        H2("Actions"),
        P(
            A(
                "Email preview",
                href="/dq/scorecard/email",
                _id="dq-scorecard-email-link",
            ),
            " · ",
            A(
                "Export HTML",
                href="/dq/scorecard/export",
                _id="dq-scorecard-export-link",
            ),
        ),
        P(
            "PDF export is deferred to M6.1 — v1 serves HTML and the "
            "browser's print-to-PDF takes it from there.",
            cls="aslan-scorecard-actions-note",
        ),
        _id="dq-scorecard-actions",
    )
    return Div(
        H1("Data Quality — Scorecard"),
        week_section,
        history_section,
        actions_section,
    )


register_template(DqScorecardVM, _build_dq_scorecard_body)


# ── Routes ────────────────────────────────────────────────────────


@app.get("/dq/scorecard")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_scorecard(request: Request) -> HTMLResponse:
    """Current-week metrics + history + email-preview + export buttons."""
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.dq_scorecard(session)
    return render(request, vm)


@app.get("/dq/scorecard/email")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_scorecard_email_preview(request: Request) -> Response:
    """Render the latest scorecard's email body inline.

    This is the exact HTML the cron handed to the email sink — pulled
    from ``audit.event(event_type='scorecard_generated').payload.body_html``.
    Rendering inline (not as a download) keeps the operator-facing
    preview behaviour (open in browser, eyeball the body, close).
    """
    _ = request
    factory = get_session_factory()
    async with factory() as session:
        body = await queries.dq_scorecard_latest_email_body(session)
    if body is None:
        # Fall back to live-rendering the most-recent week's table.
        async with factory() as session:
            vm = await queries.dq_scorecard(session)
        if not vm.rows:
            return HTMLResponse(
                "<h1>No scorecard email body available</h1>"
                "<p>Run <code>aslan-core audit scorecard</code> to generate "
                "a weekly digest.</p>",
                status_code=200,
            )
        rows = [
            scorecard_module.ScorecardRow(
                metric_name=r.metric_name,
                target=r.target,
                actual=r.actual,
                status=r.status,
                notes=r.notes,
            )
            for r in vm.rows
        ]
        _, fallback_body = scorecard_module.render_email(
            week_start=vm.current_week_start.date(), rows=rows
        )
        return HTMLResponse(fallback_body, status_code=200)
    return HTMLResponse(body, status_code=200)


@app.get("/dq/scorecard/export")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_scorecard_export(request: Request) -> Response:
    """Download the current week's scorecard as a standalone HTML file.

    Filename: ``aslan-scorecard-YYYY-MM-DD.html`` where the date is the
    week_start. Real PDF export is M6.1 deferred (needs WeasyPrint /
    reportlab); v1 ships HTML and the browser's print-to-PDF.
    """
    _ = request
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.dq_scorecard(session)
    if not vm.rows:
        return HTMLResponse(
            "<h1>No scorecard yet</h1>"
            "<p>Run <code>aslan-core audit scorecard</code> to generate "
            "this week's digest.</p>",
            status_code=200,
        )
    rows = [
        scorecard_module.ScorecardRow(
            metric_name=r.metric_name,
            target=r.target,
            actual=r.actual,
            status=r.status,
            notes=r.notes,
        )
        for r in vm.rows
    ]
    week_start = vm.current_week_start.date()
    body = scorecard_module.render_html(week_start=week_start, rows=rows)
    filename = f"aslan-scorecard-{week_start.isoformat()}.html"
    return Response(
        content=body,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
