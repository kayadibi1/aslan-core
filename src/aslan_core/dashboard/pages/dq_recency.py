"""/dq/recency — per-source recency view (M0 stub).

M1 will replace the body with the recency table backed by
``dq.sync_log`` and the per-source SLA configs from spec §10.3.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

# Side-effect import: registers the shared DqStubVM template against
# ``DqStubVM`` so render() can find a builder regardless of which dq
# page is hit first.
import aslan_core.dashboard.pages.dq_overview  # noqa: F401
from aslan_core.dashboard.app import app
from aslan_core.dashboard.render import render
from aslan_core.dashboard.view_models import DqStubVM


@app.get("/dq/recency")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_recency(request: Request) -> HTMLResponse:
    vm = DqStubVM(
        title="Data Quality — Recency",
        body_message="M0 scaffold. Recency table will land in M1.",
    )
    return render(request, vm)
