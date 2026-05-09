"""/dq/recency — per-source recency view (M0 stub).

M1 will replace the body with the recency table backed by
``dq.sync_log`` and the per-source SLA configs from spec §10.3.
"""

from __future__ import annotations

from fasthtml.common import H1, Div, P
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard.app import app


@app.get("/dq/recency")  # type: ignore[misc,untyped-decorator,unused-ignore]
def dq_recency(request: Request) -> HTMLResponse:
    _ = request
    body = Div(
        H1("Data Quality — Recency"),
        P("M0 scaffold. Recency table will land in M1.", _id="dq-stub"),
    )
    return HTMLResponse(str(body))
