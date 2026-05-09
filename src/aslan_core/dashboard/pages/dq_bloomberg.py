"""/dq/bloomberg — Bloomberg-comparison surface (M0 stub).

M4 will replace the body with the Bloomberg-vs-Aslan diff matrix
per spec §10.6.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

# Side-effect import: registers the shared DqStubVM template.
import aslan_core.dashboard.pages.dq_overview  # noqa: F401
from aslan_core.dashboard.app import app
from aslan_core.dashboard.render import render
from aslan_core.dashboard.view_models import DqStubVM


@app.get("/dq/bloomberg")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_bloomberg(request: Request) -> HTMLResponse:
    vm = DqStubVM(
        title="Data Quality — Bloomberg Comparison",
        body_message="M0 scaffold. Bloomberg comparison will land in M4.",
    )
    return render(request, vm)
