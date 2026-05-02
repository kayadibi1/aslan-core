"""Redactions page — ``/redactions``.

Spec §4.9: ``streams.redaction_registry`` cross-referenced with
``streams.event_id_to_redis`` to show "this redaction has N Redis
copies; M are XDEL'd." ``redacted_payload`` is forbidden — only
the SHA hashes and timestamps leave the database. The whole point
of the registry view is verifying the *operation*, not exposing
the payload that motivated it.

Like the audit page, this view carries a standing compliance
banner because the v0.6.0 deployment does not provide per-view
operator attribution. Spec §6.2.

Note: the spec text mentions ``redacted_payload`` as a forbidden
column, but migration 0020 already drops the GRANT — even an
attacker who reaches ``connection.exec_driver_sql`` cannot read
it. The page-level discipline below is the structural backstop.
"""

from __future__ import annotations

from fasthtml.common import H1, Div, P, Table, Tbody, Td, Th, Thead, Tr
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_session_factory
from aslan_core.dashboard.formatters import format_id_prefix
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import RedactionRowVM, RedactionsVM


def _compliance_banner(text: str) -> object:
    """Same shape as the audit page's banner. Duplicated rather than
    shared so a future refactor that hides one banner does not also
    hide the other — the duplication is a safety feature, not
    technical debt."""
    return Div(P(text), cls="aslan-compliance-banner", role="alert")


def _build_redactions_body(vm: RedactionsVM) -> object:
    headers = (
        "event_id",
        "reason",
        "stream",
        "redacted_at",
        "original_hash",
        "redacted_hash",
        "redis_copies",
    )
    return Div(
        _compliance_banner(vm.compliance_banner),
        H1("Redactions"),
        Table(
            Thead(Tr(*[Th(h) for h in headers])),
            Tbody(*[_redaction_row(r) for r in vm.rows]),
            cls="aslan-table",
        ),
        cls="aslan-redactions",
    )


def _redaction_row(row: RedactionRowVM) -> object:
    copies = f"{row.redis_copies_xdeled}/{row.redis_copies_total}"
    return Tr(
        Td(format_id_prefix(str(row.event_id))),
        Td(row.redaction_reason),
        Td(row.original_stream),
        Td(row.redacted_at.isoformat()),
        Td(format_id_prefix(row.original_payload_hash, width=12)),
        Td(format_id_prefix(row.redacted_payload_hash, width=12)),
        Td(copies),
    )


register_template(RedactionsVM, _build_redactions_body)


@app.get("/redactions")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def redactions_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.redactions_recent(session, limit=50)
    return render(request, vm)
