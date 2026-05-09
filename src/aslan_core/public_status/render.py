"""Minimal HTML scaffolding for the public ``/status`` page.

Self-contained — no nav (single-page), no htmx (server-rendered, page
refreshes on browser reload, CDN-cacheable for 60s), no external
stylesheets (one inline <style> block). Keeps the footprint small
enough that the entire response fits in one TCP segment for most
viewports.

The styling is intentionally restrained — institutional, not flashy.
Aslan Terminal's mission bar (workspace CLAUDE.md) makes "Bloomberg-
caliber TR coverage" the value proposition; the public status page
should look like it belongs in that lineage, not like a startup
landing page.
"""

from __future__ import annotations

from fasthtml.common import (
    Body,
    Head,
    Html,
    Meta,
    Style,
    Title,
    to_xml,
)
from starlette.responses import HTMLResponse

from aslan_core.public_status.view_models import PublicStatusVM, _PublicVMBase

_INLINE_STYLE = """
:root {
    --bg: #0e1116;
    --fg: #e7eaef;
    --muted: #8a93a3;
    --card: #161a22;
    --border: #232936;
    --ok: #2ea44f;
    --warn: #d29922;
    --crit: #cf222e;
    --unknown: #57606a;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--fg);
    line-height: 1.5;
}
.aslan-status-wrap {
    max-width: 720px;
    margin: 0 auto;
    padding: 48px 24px;
}
.aslan-status-headline {
    font-size: 28px;
    font-weight: 600;
    margin: 0 0 4px 0;
}
.aslan-status-sub {
    color: var(--muted);
    font-size: 14px;
    margin: 0 0 32px 0;
}
.aslan-status-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 20px;
    margin: 12px 0;
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: center;
    gap: 16px;
}
.aslan-status-source {
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 14px;
    min-width: 60px;
}
.aslan-status-meta {
    color: var(--muted);
    font-size: 13px;
}
.aslan-status-meta strong {
    color: var(--fg);
    font-weight: 500;
}
.aslan-status-badge {
    display: inline-block;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}
.aslan-status-badge-ok { background: rgba(46, 164, 79, 0.15); color: var(--ok); }
.aslan-status-badge-warn { background: rgba(210, 153, 34, 0.15); color: var(--warn); }
.aslan-status-badge-crit { background: rgba(207, 34, 46, 0.15); color: var(--crit); }
.aslan-status-badge-unknown { background: rgba(87, 96, 106, 0.2); color: var(--unknown); }
.aslan-status-footer {
    margin-top: 40px;
    padding-top: 20px;
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 12px;
    text-align: center;
}
.aslan-status-footer a { color: var(--muted); text-decoration: underline; }
"""


# Cache-Control: a CDN can absorb most public-status traffic. The
# data freshness target is 5 min (cron writes recency every 5 min),
# so a 60s cache hits the right balance — most visitors see a cached
# response, the page is never more than 60s out of date.
_CACHE_CONTROL: str = "public, max-age=60"


def base_template(*, title: str, body: object) -> object:
    """Wrap ``body`` in the public-status HTML skeleton.

    NO nav, NO htmx, NO external stylesheets. Inline <style> only.
    """
    return Html(
        Head(
            Title(title),
            Meta(charset="utf-8"),
            Meta(
                name="viewport",
                content="width=device-width, initial-scale=1",
            ),
            Meta(name="robots", content="index, follow"),
            Style(_INLINE_STYLE),
        ),
        Body(body),
    )


def render(vm: _PublicVMBase, *, body_builder: object) -> HTMLResponse:
    """Render ``vm`` to an ``HTMLResponse`` with public-cache headers.

    Mirrors :func:`aslan_core.dashboard.render.render` discipline:

      * runtime-checks ``isinstance(vm, _PublicVMBase)`` so a future
        handler accidentally passing a non-public VM type fails loudly;
      * calls ``vm.model_dump(mode='json')`` to surface non-JSON-safe
        fields at runtime;
      * the body builder is supplied by the caller (single page, no
        registry needed).
    """
    if not isinstance(vm, _PublicVMBase):
        raise TypeError(
            f"public_status render() requires a _PublicVMBase subclass; got {type(vm).__name__}."
        )
    vm.model_dump(mode="json")

    if not callable(body_builder):
        raise TypeError(
            "public_status render() body_builder must be callable; "
            f"got {type(body_builder).__name__}."
        )
    body = body_builder(vm)
    page = base_template(title="Aslan Terminal — System Status", body=body)
    html = to_xml(page)
    return HTMLResponse(
        html,
        headers={"Cache-Control": _CACHE_CONTROL},
    )


def title_for_vm(vm: PublicStatusVM) -> str:
    """Compute a short ``<title>`` reflecting the headline state.

    Helps customers pinned the tab — "Aslan: 98% Fresh" beats
    "Aslan Terminal — System Status" when the tab is collapsed.
    """
    return f"Aslan: {vm.overall_freshness_pct:.0f}% Fresh"


__all__ = ["base_template", "render", "title_for_vm"]
