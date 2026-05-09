"""/dq/validation — validation-failure browser (M0 stub).

M5 will replace the body with the validation-failure browser backed
by ``dq.validation_failure`` per spec §10.7.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

# Side-effect import: registers the shared DqStubVM template.
import aslan_core.dashboard.pages.dq_overview  # noqa: F401
from aslan_core.dashboard.app import app
from aslan_core.dashboard.render import render
from aslan_core.dashboard.view_models import DqStubVM


@app.get("/dq/validation")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_validation(request: Request) -> HTMLResponse:
    vm = DqStubVM(
        title="Data Quality — Validation",
        body_message="M0 scaffold. Validation browser will land in M5.",
    )
    return render(request, vm)
