"""/dq/coverage — roster + field coverage view (M0 stub).

M1 will replace the body with the coverage tables backed by
``dq.coverage_snapshot`` per spec §10.4.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

# Side-effect import: registers the shared DqStubVM template.
import aslan_core.dashboard.pages.dq_overview  # noqa: F401
from aslan_core.dashboard.app import app
from aslan_core.dashboard.render import render
from aslan_core.dashboard.view_models import DqStubVM


@app.get("/dq/coverage")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_coverage(request: Request) -> HTMLResponse:
    vm = DqStubVM(
        title="Data Quality — Coverage",
        body_message="M0 scaffold. Coverage tables will land in M1.",
    )
    return render(request, vm)
