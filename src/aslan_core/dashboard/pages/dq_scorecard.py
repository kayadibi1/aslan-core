"""/dq/scorecard — weekly DQ scorecard (M0 stub).

M6 will replace the body with the weekly scorecard + email-digest
preview per spec §10.8.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

# Side-effect import: registers the shared DqStubVM template.
import aslan_core.dashboard.pages.dq_overview  # noqa: F401
from aslan_core.dashboard.app import app
from aslan_core.dashboard.render import render
from aslan_core.dashboard.view_models import DqStubVM


@app.get("/dq/scorecard")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_scorecard(request: Request) -> HTMLResponse:
    vm = DqStubVM(
        title="Data Quality — Scorecard",
        body_message="M0 scaffold. Weekly scorecard will land in M6.",
    )
    return render(request, vm)
