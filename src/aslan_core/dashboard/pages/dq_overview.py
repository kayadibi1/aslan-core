"""/dq/overview — operational-audit roll-up heatmap (M0 stub).

M1 will replace the body with the 5x4 heatmap from spec §10.2.
"""

from __future__ import annotations

from fasthtml.common import H1, Div, P
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard.app import app
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import DqStubVM


def _build_dq_stub_body(vm: DqStubVM) -> object:
    """Shared body builder for every M0 /dq/* stub page.

    Registered once against ``DqStubVM``; every dq page passes a VM
    of this type. ``stub_id`` is rendered into the ``<p>`` element's
    ``id`` attribute so the scaffold smoke test
    (``test_dq_route_returns_200``) can grep for ``dq-stub`` in the
    response without depending on rendered chrome.
    """
    return Div(
        H1(vm.title),
        P(vm.body_message, _id=vm.stub_id),
    )


register_template(DqStubVM, _build_dq_stub_body)


@app.get("/dq/overview")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_overview(request: Request) -> HTMLResponse:
    vm = DqStubVM(
        title="Data Quality — Overview",
        body_message="M0 scaffold. Heatmap will land in M1.",
    )
    return render(request, vm)
