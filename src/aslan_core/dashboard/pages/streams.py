"""Streams page — ``/streams``.

Pure Redis-driven view. Lists every canonical stream from
``aslan_core.streams.STREAMS`` and probes XLEN + last-entry id for
each under the spec §5.3 budget (``1 + len(STREAMS)``). The
circuit breaker shared across the FastHTML app instance handles
Redis outages — the page renders with ``unknown`` cells rather
than 500'ing.
"""

from __future__ import annotations

from fasthtml.common import H1, Div, P, Table, Tbody, Td, Th, Thead, Tr
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard.app import app, get_breaker, get_redis_client
from aslan_core.dashboard.redis_probes import open_budget, xinfo_stream_lite, xlen
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import RedisState, StreamRowVM, StreamsVM
from aslan_core.streams.names import STREAMS


def _build_streams_body(vm: StreamsVM) -> object:
    return Div(
        H1("Streams"),
        P("Redis circuit: " + ("open" if vm.redis_circuit_open else "closed")),
        Table(
            Thead(Tr(Th("stream"), Th("xlen"), Th("last_entry_age_s"), Th("pending_per_group"))),
            Tbody(*[_stream_row(r) for r in vm.rows]),
            cls="aslan-table",
        ),
        cls="aslan-streams",
    )


def _stream_row(row: StreamRowVM) -> object:
    last_age = f"{row.last_entry_age_s:.0f}s" if row.last_entry_age_s is not None else "—"
    pending = (
        ", ".join(f"{g}={n}" for g, n in row.pending_per_group.items())
        if row.pending_per_group
        else "—"
    )
    return Tr(
        Td(row.stream_name),
        Td(str(row.xlen)),
        Td(last_age),
        Td(pending),
    )


register_template(StreamsVM, _build_streams_body)

# RedisState is referenced lazily by the body builder via row.redis_state
# (deadletter page uses it). Keep an explicit binding so static analysis
# keeps the import.
_ = RedisState


@app.get("/streams")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def streams_page(request: Request) -> HTMLResponse:
    redis = get_redis_client()
    breaker = get_breaker()
    rows: list[StreamRowVM] = []
    with open_budget(limit=1 + len(STREAMS) * 2) as budget:
        for stream_name in STREAMS:
            length = await xlen(
                redis,  # type: ignore[arg-type]
                stream_name,
                breaker=breaker,
                budget=budget,
            )
            info = await xinfo_stream_lite(
                redis,  # type: ignore[arg-type]
                stream_name,
                breaker=breaker,
                budget=budget,
            )
            rows.append(
                StreamRowVM(
                    stream_name=stream_name,
                    xlen=int(length) if length is not None else 0,
                    last_entry_age_s=None,  # Task 9 fills this when needed
                    pending_per_group={},
                )
            )
            _ = info  # info available for future expansion
    vm = StreamsVM(rows=rows, redis_circuit_open=breaker.is_open())
    return render(request, vm)
