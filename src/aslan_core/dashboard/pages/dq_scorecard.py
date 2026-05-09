"""/dq/scorecard — weekly DQ scorecard (M0 stub).

M6 will replace the body with the weekly scorecard + email-digest
preview per spec §10.8.
"""

from __future__ import annotations

from fasthtml.common import H1, Div, P
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard.app import app


@app.get("/dq/scorecard")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_scorecard(request: Request) -> HTMLResponse:
    _ = request
    body = Div(
        H1("Data Quality — Scorecard"),
        P("M0 scaffold. Weekly scorecard will land in M6.", _id="dq-stub"),
    )
    return HTMLResponse(str(body))
