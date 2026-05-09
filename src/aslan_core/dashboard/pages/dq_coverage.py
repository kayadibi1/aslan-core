"""/dq/coverage — roster + field coverage view (M0 stub).

M1 will replace the body with the coverage tables backed by
``dq.coverage_snapshot`` per spec §10.4.
"""

from __future__ import annotations

from fasthtml.common import H1, Div, P
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard.app import app


@app.get("/dq/coverage")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_coverage(request: Request) -> HTMLResponse:
    _ = request
    body = Div(
        H1("Data Quality — Coverage"),
        P("M0 scaffold. Coverage tables will land in M1.", _id="dq-stub"),
    )
    return HTMLResponse(str(body))
