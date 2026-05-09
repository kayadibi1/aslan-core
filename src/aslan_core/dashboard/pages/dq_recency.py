"""/dq/recency — per-source recency lag time-series.

For each source x bucket (1h / 24h / 7d / 30d), tabulates avg / p95 /
max lag in seconds plus the breach count. Backed by
`audit.recency_observation` aggregates computed server-side via
`percentile_cont`.
"""

from __future__ import annotations

from fasthtml.common import H1, H2, Div, Table, Td, Th, Tr
from starlette.requests import Request
from starlette.responses import HTMLResponse

# Side-effect import: the shared DqStubVM template lives in dq_overview.
import aslan_core.dashboard.pages.dq_overview  # noqa: F401
from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_session_factory
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import DqRecencyRowVM, DqRecencyVM

_BUCKETS = ("1h", "24h", "7d", "30d")
_BUCKET_LABELS = {
    "1h": "Last 1 h",
    "24h": "Last 24 h",
    "7d": "Last 7 d",
    "30d": "Last 30 d",
}


def _fmt_seconds(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{int(value)}s" if isinstance(value, int) else f"{value:.1f}s"


def _build_dq_recency_body(vm: DqRecencyVM) -> object:
    by_bucket: dict[str, list[DqRecencyRowVM]] = {b: [] for b in _BUCKETS}
    for row in vm.rows:
        by_bucket[row.bucket].append(row)
    sections = []
    for bucket in _BUCKETS:
        rows = by_bucket[bucket]
        header = Tr(
            Th("Source"),
            Th("Avg lag"),
            Th("p95 lag"),
            Th("Max lag"),
            Th("Breach count"),
        )
        body_rows: list[object] = []
        for row in rows:
            body_rows.append(
                Tr(
                    Th(row.source.upper()),
                    Td(_fmt_seconds(row.avg_lag_seconds)),
                    Td(_fmt_seconds(row.p95_lag_seconds)),
                    Td(_fmt_seconds(row.max_lag_seconds)),
                    Td(str(row.breach_count)),
                )
            )
        if not body_rows:
            body_rows = [Tr(Td("No observations in window", colspan="5"))]
        sections.append(
            Div(
                H2(_BUCKET_LABELS[bucket]),
                Table(header, *body_rows, cls="aslan-recency-table"),
                cls="aslan-recency-section",
                _id=f"recency-{bucket}",
            )
        )
    return Div(H1("Data Quality — Recency"), *sections)


register_template(DqRecencyVM, _build_dq_recency_body)


@app.get("/dq/recency")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_recency(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.dq_recency(session)
    return render(request, vm)
