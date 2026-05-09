"""/dq/validation — validation-failure browser (M0 stub).

M5 will replace the body with the validation-failure browser backed
by ``dq.validation_failure`` per spec §10.7.
"""

from __future__ import annotations

from fasthtml.common import H1, Div, P
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard.app import app


@app.get("/dq/validation")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_validation(request: Request) -> HTMLResponse:
    _ = request
    body = Div(
        H1("Data Quality — Validation"),
        P("M0 scaffold. Validation browser will land in M5.", _id="dq-stub"),
    )
    return HTMLResponse(str(body))
