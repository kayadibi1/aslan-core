"""Dead-letter page — ``/deadletter``.

Spec §4.3: metadata-only view of ``streams.deadletter_log`` LEFT-JOIN
``streams.deadletter_redis_index``. Per-row ``redis_state`` is
derived from O(1) probes only — no per-row Redis lookup. Codex
round-1 explicitly forbade ``XRANGE`` for this view because per-row
probes multiply under pagination + polling.

The state-derivation rules (codex round-1 deferred to v0.6.0
implementation):

  * ``MISSING_INDEX`` — ``has_redis_index`` is false OR
    ``redis_message_id`` is NULL. The durable index in
    ``deadletter_redis_index`` was never written; either the janitor
    hasn't reconciled or the XADD failed mid-flight.
  * ``UNKNOWN`` — circuit breaker is open OR the per-stream probe
    returned ``None`` (timeout, error, budget exhausted).
  * ``PRESENT`` — index row exists AND probe returned a non-empty
    stream length. Best-effort O(1) signal that the message is at
    least *not yet trimmed* — confirming it is the specific message
    would require ``XRANGE`` which is forbidden.
  * ``TRIMMED`` — not derivable from O(1) probes alone; the enum
    value is reachable only when the stream length is 0 (i.e. the
    operator can be confident every message is gone). For non-empty
    streams we report ``PRESENT`` even if a specific message_id was
    trimmed — operators verifying a specific failure_id use
    ``aslan deadletter inspect`` from the CLI.

Mutating affordances (redrive button) are absent in v0.6.0 — the
page lists redrive-eligible rows so operators can spot them, but
the action runs from the CLI host where the OS user is the durable
actor. See spec §4.3 line 164.
"""

from __future__ import annotations

from typing import Any

from fasthtml.common import H1, Div, P, Table, Tbody, Td, Th, Thead, Tr
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_breaker, get_redis_client, get_session_factory
from aslan_core.dashboard.formatters import format_id_prefix
from aslan_core.dashboard.redis_probes import open_budget, xinfo_stream_lite
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import DeadletterRowVM, DeadletterVM, RedisState
from aslan_core.streams.names import STREAMS


def _build_deadletter_body(vm: DeadletterVM) -> object:
    headers = (
        "failure_id",
        "stream",
        "group",
        "consumer",
        "event_id",
        "failures",
        "routed_at",
        "redis_state",
    )
    return Div(
        H1("Deadletter"),
        P(f"Total: {vm.total}"),
        Table(
            Thead(Tr(*[Th(h) for h in headers])),
            Tbody(*[_deadletter_row(r) for r in vm.rows]),
            cls="aslan-table",
        ),
        cls="aslan-deadletter",
    )


def _deadletter_row(row: DeadletterRowVM) -> object:
    return Tr(
        Td(str(row.failure_id)),
        Td(row.stream_name),
        Td(row.group_name),
        Td(row.consumer_name),
        Td(format_id_prefix(str(row.event_id))),
        Td(str(row.failure_count)),
        Td(row.routed_at.isoformat()),
        Td(row.redis_state.value),
    )


register_template(DeadletterVM, _build_deadletter_body)


def _classify_state(
    *,
    row: dict[str, Any],
    stream_probe: dict[str, Any] | None,
    breaker_open: bool,
) -> RedisState:
    """Derive ``RedisState`` for a single deadletter row using O(1)
    inputs only. See module docstring for rules."""
    if not row.get("has_redis_index") or row.get("redis_message_id") is None:
        return RedisState.MISSING_INDEX
    if breaker_open or stream_probe is None:
        return RedisState.UNKNOWN
    if int(stream_probe.get("length", 0)) == 0:
        # Empty stream — every message_id is gone.
        return RedisState.TRIMMED
    return RedisState.PRESENT


@app.get("/deadletter")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def deadletter_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        rows, total = await queries.deadletter_recent(session, limit=50, offset=0)

    redis = get_redis_client()
    breaker = get_breaker()

    # Per-stream probe budget: 1 + len(STREAMS). The deadletter view
    # collapses per-row probes into per-unique-deadletter-stream
    # probes, so the cap holds even with thousands of failure rows.
    stream_probes: dict[str, dict[str, Any] | None] = {}
    with open_budget(limit=1 + len(STREAMS)) as budget:
        for row in rows:
            ds = row["deadletter_stream"]
            if ds in stream_probes:
                continue
            stream_probes[ds] = await xinfo_stream_lite(
                redis,  # type: ignore[arg-type]
                ds,
                breaker=breaker,
                budget=budget,
            )

    breaker_open = breaker.is_open()
    deadletter_rows = [
        DeadletterRowVM(
            failure_id=row["failure_id"],
            event_id=row["event_id"],
            stream_name=row["stream_name"],
            group_name=row["group_name"],
            consumer_name=row["consumer_name"],
            failure_count=row["failure_count"],
            last_error_kind=row["last_error_kind"],
            routed_at=row["routed_at"],
            routed_at_redis=row["routed_at_redis"],
            redis_message_id=row["redis_message_id"],
            redis_state=_classify_state(
                row=row,
                stream_probe=stream_probes.get(row["deadletter_stream"]),
                breaker_open=breaker_open,
            ),
        )
        for row in rows
    ]
    vm = DeadletterVM(rows=deadletter_rows, total=total)
    return render(request, vm)
