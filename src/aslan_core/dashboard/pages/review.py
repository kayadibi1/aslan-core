"""Review page — ``/review``.

Per aslan-event-extractor SCOPE_v2 D26 / M0: depth counters across
the three extractor-owned review queues. Skeleton — no per-row
resolution UI yet (lands in extractor M3-M5 when there's data to
act on and the counters justify a richer view).

Mirrors the patterns in `overview.py`: SQL-only via the
`queries.review_overview` helper; render via the registered
template; no implicit `__str__` / `__repr__` on the VM.
"""

from __future__ import annotations

from fasthtml.common import H1, H2, Div, P, Section
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_session_factory
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import ReviewVM


def _build_review_body(vm: ReviewVM) -> object:
    """Three cards, one per queue, with an explanation of what each holds."""
    return Div(
        H1("Review"),
        Section(
            Div(
                H2("Review queue"),
                P(f"Unresolved: {vm.review_queue_pending}"),
                P(
                    "Tier 1 / Tier 2 disagreements awaiting human resolution. "
                    "Resolution UI lands in M3-M5."
                ),
                cls="aslan-card",
            ),
            Div(
                H2("Entity resolution"),
                P(f"Pending: {vm.entity_resolution_pending}"),
                P("Counterparty / mentioned-entity name lookups awaiting registry match."),
                cls="aslan-card",
            ),
            Div(
                H2("Quarantine"),
                P(f"Total: {vm.quarantine_total}"),
                P(
                    "Filings the extractor couldn't process — oversized, "
                    "parse-failed, or low-confidence."
                ),
                cls="aslan-card",
            ),
            cls="aslan-cards",
        ),
        cls="aslan-review",
    )


register_template(ReviewVM, _build_review_body)


@app.get("/review")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def review_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.review_overview(session)
    return render(request, vm)
