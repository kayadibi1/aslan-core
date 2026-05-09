"""/dq/bloomberg — Bloomberg-comparison surface (M0 stub).

M4 will replace the body with the Bloomberg-vs-Aslan diff matrix
per spec §10.6.
"""

from __future__ import annotations

from fasthtml.common import H1, Div, P
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard.app import app


@app.get("/dq/bloomberg")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_bloomberg(request: Request) -> HTMLResponse:
    _ = request
    body = Div(
        H1("Data Quality — Bloomberg Comparison"),
        P("M0 scaffold. Bloomberg comparison will land in M4.", _id="dq-stub"),
    )
    return HTMLResponse(str(body))
