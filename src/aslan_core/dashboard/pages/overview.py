"""Overview page — landing route ``/``.

Renders headline counters across every subsystem the dashboard
covers: outbox / streams / deadletter / ingestion / audit /
redactions. The SQL-derived fields come from
``queries.overview``; ``streams_total_xlen`` is a Redis-derived
field and is filled in here from a bounded ``xlen`` probe across
``aslan_core.streams.STREAMS``.

The handler:

  * opens the dashboard-role session;
  * calls ``queries.overview`` for the SQL-derived VM (with
    ``streams_total_xlen=0`` placeholder);
  * runs O(1) ``xlen`` probes per stream under a fresh budget +
    the shared circuit breaker;
  * constructs the final ``OverviewVM`` via ``model_copy`` (frozen
    Pydantic) with ``streams_total_xlen`` filled in;
  * returns ``render(request, vm)``.
"""

from __future__ import annotations

from fasthtml.common import H1, H2, Div, P, Section
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard import queries
from aslan_core.dashboard.app import (
    app,
    get_breaker,
    get_redis_client,
    get_session_factory,
)
from aslan_core.dashboard.formatters import format_age_s
from aslan_core.dashboard.redis_probes import open_budget, xlen
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import OverviewVM
from aslan_core.streams.names import STREAMS


def _build_overview_body(vm: OverviewVM) -> object:
    """Build the FastHTML element tree for the overview page body.

    Renders 6 cards: outbox, streams, deadletter, ingestion, audit,
    redactions. Each card pulls only typed fields from ``vm`` —
    no implicit ``str(vm)`` / ``repr(vm)`` (codex round-2 contract).
    """
    return Div(
        H1("Overview"),
        Section(
            Div(
                H2("Outbox"),
                P(f"Pending: {vm.outbox_pending}"),
                P(f"Oldest pending age: {format_age_s(vm.outbox_oldest_age_s)}"),
                P(f"Drained last 15m: {vm.outbox_drained_last_15m}"),
                cls="aslan-card",
            ),
            Div(
                H2("Streams"),
                P(f"Total XLEN: {vm.streams_total_xlen}"),
                cls="aslan-card",
            ),
            Div(
                H2("Deadletter"),
                P(f"Total: {vm.deadletter_total}"),
                P(f"Last 24h: {vm.deadletter_last_24h}"),
                cls="aslan-card",
            ),
            Div(
                H2("Ingestion"),
                P(f"Runs last 24h: {vm.ingestion_runs_last_24h}"),
                cls="aslan-card",
            ),
            Div(
                H2("Audit"),
                P(f"Events/min last 60m: {vm.audit_events_per_min_last_60m:.2f}"),
                cls="aslan-card",
            ),
            Div(
                H2("Redactions"),
                P(f"Total: {vm.redaction_registry_size}"),
                P(
                    "Last redaction: "
                    + (
                        vm.redaction_last_at.isoformat()
                        if vm.redaction_last_at is not None
                        else "—"
                    )
                ),
                cls="aslan-card",
            ),
            cls="aslan-cards",
        ),
        cls="aslan-overview",
    )


# Register the body builder so render(request, vm) finds it.
register_template(OverviewVM, _build_overview_body)


@app.get("/")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def overview_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    redis = get_redis_client()
    breaker = get_breaker()

    async with factory() as session:
        sql_vm = await queries.overview(session)

    # Bounded Redis fan-out — one xlen per registered stream, capped
    # by the spec §5.3 budget (1 + len(STREAMS)). The runtime ``Redis``
    # client's wider signature doesn't structurally match the narrow
    # ``_RedisLike`` Protocol (same gap covered for the boundary tests
    # in Task 6); the type-ignore reflects the runtime contract.
    total_xlen = 0
    with open_budget(limit=1 + len(STREAMS)) as budget:
        for stream_name in STREAMS:
            length = await xlen(
                redis,  # type: ignore[arg-type]
                stream_name,
                breaker=breaker,
                budget=budget,
            )
            if length is not None:
                total_xlen += length

    vm = sql_vm.model_copy(update={"streams_total_xlen": total_xlen})
    return render(request, vm)
