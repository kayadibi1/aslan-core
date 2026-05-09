"""/dq/overview — operational-audit roll-up heatmap (M0 stub).

M1 will replace the body with the 5x4 heatmap from spec §10.2.
"""

from __future__ import annotations

from fasthtml.common import H1, Div, P
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard.app import app


@app.get("/dq/overview")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_overview(request: Request) -> HTMLResponse:
    _ = request
    body = Div(
        H1("Data Quality — Overview"),
        P("M0 scaffold. Heatmap will land in M1.", _id="dq-stub"),
    )
    return HTMLResponse(str(body))
