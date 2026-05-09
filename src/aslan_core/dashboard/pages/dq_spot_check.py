"""/dq/spot-check — manual spot-check workflow (M0 stub).

M2 will replace the body with the spot-check queue + reviewer
workflow per spec §10.5.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

# Side-effect import: registers the shared DqStubVM template.
import aslan_core.dashboard.pages.dq_overview  # noqa: F401
from aslan_core.dashboard.app import app
from aslan_core.dashboard.render import render
from aslan_core.dashboard.view_models import DqStubVM


@app.get("/dq/spot-check")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_spot_check(request: Request) -> HTMLResponse:
    vm = DqStubVM(
        title="Data Quality — Spot Check",
        body_message="M0 scaffold. Spot-check workflow will land in M2.",
    )
    return render(request, vm)
