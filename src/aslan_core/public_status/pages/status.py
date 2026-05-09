"""``/status`` — public, no-auth landing page for the dq subsystem.

Renders the headline freshness, the per-source rows (KAP, EVDS, BIST,
TEFAS, MKK), and the footer. No auth gate, no header check. The
``Cache-Control`` header is set by :func:`aslan_core.public_status.render.render`.
"""

from __future__ import annotations

from fasthtml.common import (
    H1,
    A,
    Div,
    P,
    Span,
)
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.public_status import queries
from aslan_core.public_status.app import app, get_session_factory
from aslan_core.public_status.render import render
from aslan_core.public_status.view_models import (
    PublicSourceRowVM,
    PublicStatusVM,
    StatusBadge,
)

# Display labels — KAP / EVDS / BIST / TEFAS / MKK are the canonical
# Turkish-finance acronyms; render in upper-case to match conventional
# usage.
_SOURCE_LABELS: dict[str, str] = {
    "kap": "KAP",
    "evds": "EVDS",
    "bist": "BIST",
    "tefas": "TEFAS",
    "mkk": "MKK",
}

# Closed mapping from VM badge enum to CSS class. Keeps the page
# template purely typed — no string concatenation against arbitrary
# enum values.
_BADGE_CSS: dict[StatusBadge, str] = {
    StatusBadge.OK: "aslan-status-badge aslan-status-badge-ok",
    StatusBadge.WARN: "aslan-status-badge aslan-status-badge-warn",
    StatusBadge.CRIT: "aslan-status-badge aslan-status-badge-crit",
    StatusBadge.UNKNOWN: "aslan-status-badge aslan-status-badge-unknown",
}

_BADGE_GLYPH: dict[StatusBadge, str] = {
    StatusBadge.OK: "Operational",
    StatusBadge.WARN: "Degraded",
    StatusBadge.CRIT: "Outage",
    StatusBadge.UNKNOWN: "Unknown",
}


def _build_row(vm: PublicSourceRowVM) -> object:
    """Build the FastHTML element tree for one source row."""
    cov_text = f"coverage {vm.coverage_pct:.1f}%" if vm.coverage_pct is not None else "coverage —"
    fresh_text = f"freshness {vm.freshness_pct:.0f}%"
    return Div(
        Span(_SOURCE_LABELS.get(vm.source, vm.source.upper()), cls="aslan-status-source"),
        P(
            Span("Last update: ", cls=""),
            Span(vm.last_update_age_label, cls=""),
            Span(f" · {fresh_text} · {cov_text}", cls=""),
            cls="aslan-status-meta",
        ),
        Span(
            _BADGE_GLYPH[vm.badge],
            cls=_BADGE_CSS[vm.badge],
        ),
        cls="aslan-status-card",
        _data_source=vm.source,
        _data_badge=vm.badge.value,
    )


def _build_status_body(vm: PublicStatusVM) -> object:
    """Assemble the page body: headline, rows, footer."""
    if vm.last_updated_at is not None:
        sub = f"Last refreshed at {vm.last_updated_at.isoformat(timespec='seconds')} (UTC)."
    else:
        sub = "No observations recorded yet."
    headline = f"Overall: {vm.overall_freshness_pct:.0f}% Fresh"
    rows = [_build_row(r) for r in vm.rows]
    return Div(
        H1(headline, cls="aslan-status-headline"),
        P(sub, cls="aslan-status-sub"),
        *rows,
        Div(
            P(
                "Powered by ",
                A("Aslan Terminal", href="https://aslanterminal.com"),
                " · ",
                A("Terms", href="https://aslanterminal.com/terms"),
            ),
            cls="aslan-status-footer",
        ),
        cls="aslan-status-wrap",
    )


@app.get("/status")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def status(request: Request) -> HTMLResponse:
    """Public ``/status`` route — NO auth, NO Authorization-header check.

    The route is deliberately unguarded. Privilege boundary lives at
    the PostgreSQL role layer (``public_status_reader``), not in the
    application — the role's GRANT list is the load-bearing security
    floor.
    """
    _ = request  # Unused; FastHTML / Starlette require the signature.
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.public_status(session)
    return render(vm, body_builder=_build_status_body)
