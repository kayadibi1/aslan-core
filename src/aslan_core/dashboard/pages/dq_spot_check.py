"""/dq/spot-check — manual spot-check workflow (M0 stub).

M2 will replace the body with the spot-check queue + reviewer
workflow per spec §10.5.
"""

from __future__ import annotations

from fasthtml.common import H1, Div, P
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard.app import app


@app.get("/dq/spot-check")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_spot_check(request: Request) -> HTMLResponse:
    _ = request
    body = Div(
        H1("Data Quality — Spot Check"),
        P("M0 scaffold. Spot-check workflow will land in M2.", _id="dq-stub"),
    )
    return HTMLResponse(str(body))
