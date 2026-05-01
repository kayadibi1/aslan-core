"""Audit page — ``/audit``.

Spec §4.8: ``audit.events`` projected through SECURITY DEFINER
helpers for the columns the dashboard role cannot see directly.
The raw ``client_ip`` and ``user_agent`` columns are revoked from
``aslan_dashboard`` by migration 0022; only the truncated CIDR
(``audit.event_client_ip_truncated``) and the metadata key count
(``audit.event_metadata_key_count``) leave the database. The raw
``metadata`` column was never granted.

A standing compliance banner is rendered server-side — non-
dismissible, no JavaScript, no localStorage hooks. Spec §6.2 +
§8.2: until ``aslan-service`` ships authenticated per-request
operator identity in v0.7.x, the v0.6.0 audit view is "network-
edge logging only — not compliance evidence." The banner exists
to prevent a paraphrase regression that would let operators mistake
the surface for compliance-grade per-view attribution.
"""

from __future__ import annotations

from fasthtml.common import H1, Div, P, Table, Tbody, Td, Th, Thead, Tr
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_session_factory
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import AuditRowVM, AuditVM


def _compliance_banner(text: str) -> object:
    """Server-rendered, non-dismissible banner. No JS hooks, no
    aria-dismiss, no data-dismiss attributes — adding one would
    land a forbidden token in the markup that the banner-presence
    test would catch."""
    return Div(P(text), cls="aslan-compliance-banner", role="alert")


def _build_audit_body(vm: AuditVM) -> object:
    headers = (
        "event_id",
        "occurred_at",
        "actor",
        "operation",
        "target",
        "client_ip",
        "metadata_keys",
    )
    return Div(
        _compliance_banner(vm.compliance_banner),
        H1("Audit"),
        Table(
            Thead(Tr(*[Th(h) for h in headers])),
            Tbody(*[_audit_row(r) for r in vm.rows]),
            cls="aslan-table",
        ),
        cls="aslan-audit",
    )


def _audit_row(row: AuditRowVM) -> object:
    return Tr(
        Td(str(row.event_id)),
        Td(row.occurred_at.isoformat()),
        Td(f"{row.actor_id} ({row.actor_kind})"),
        Td(row.operation),
        Td(f"{row.target_schema}.{row.target_table}"),
        Td(row.client_ip_truncated),
        Td(str(row.metadata_key_count)),
    )


register_template(AuditVM, _build_audit_body)


@app.get("/audit")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def audit_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.audit_recent(session, limit=50)
    return render(request, vm)
