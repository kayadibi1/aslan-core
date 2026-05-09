"""/dq/spot-check — pending queue + per-sample labelling form.

M2 deliverable per spec §5.5 + §7.2 + §10.5; NG6 extends the
per-sample view with an external-corroborator panel.

Two GET routes:

  * ``GET /dq/spot-check`` — pending-queue table; click-through opens
    the per-sample form.
  * ``GET /dq/spot-check/{sample_id}`` — labelling form:
      left = parsed/canonical DB row (record_pk pairs);
      right = raw bytes pane (v1 placeholder; live MinIO replay in M2.1);
      bottom = per-field text inputs + truth-value submission form;
      below all of the above (NG6) = external corroborator panel
      surfacing one block per registered firecrawl source — cache
      hit / miss / refresh-button states are surfaced to the labeller.

Two POST routes:

  * ``POST /dq/spot-check/{sample_id}`` — accept the labelling form
    submission. The handler dispatches one ``label_field()`` call per
    request, then 303-redirects back to the pending queue.
  * ``POST /dq/spot-check/{sample_id}/corroborator/{source}/refresh``
    (NG6) — force a fresh firecrawl fetch for the registered source +
    redirect back to the per-sample page so the new cache row renders.

Spec §5.5 contract: ``audit.spot_check_sample`` rows are append-only;
the labelling flow flips ``labelled = false → true`` once via the
narrow column-level UPDATE GRANT. ``audit.spot_check_result`` rows
are append-only outright. ``audit.external_corroborator_cache`` rows
are append-only outright (UPDATE never granted). The dashboard role
itself has SELECT only on all three tables (migrations 0058 / 0064);
the POST handlers run write helpers under a write-capable session,
which on production is the labeller's authenticated session against
the ``audit_writer`` role.
"""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from fasthtml.common import (
    H1,
    H2,
    A,
    Button,
    Div,
    Form,
    Input,
    Label,
    P,
    Span,
    Table,
    Td,
    Textarea,
    Th,
    Tr,
)
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

# Side-effect import: shared DqStubVM template lives in dq_overview.
import aslan_core.dashboard.pages.dq_overview  # noqa: F401
from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_session_factory
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import (
    DqSpotCheckCorroboratorPanelVM,
    DqSpotCheckQueueVM,
    DqSpotCheckSampleDetailVM,
)
from aslan_core.dq import corroborator, spot_check

# ── Input validation ─────────────────────────────────────────────


# Field names: short alphanumeric + underscore + dot. Matches the
# spec's per-field labels (e.g. `revenue_try`, `period_end`,
# `entity.name`). Length-bounded so a malicious form post can't DoS
# the persisted column.
_FIELD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,63}$")
# Corroborator source names are constrained to short alphanumeric +
# underscore so a malicious URL path cannot smuggle anything past the
# `_REGISTERED_SOURCES` membership check.
_CORROBORATOR_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,32}$")
# Truth values are textual — the column is TEXT. We bound at 4 KiB
# so a labeller-side paste error doesn't blow out the row width.
_MAX_TRUTH_VALUE_LEN = 4096
_MAX_LABEL_NOTE_LEN = 2048
_MAX_LABELLER_LEN = 64


def _validate_field(field: str) -> str | None:
    if not field:
        return "field name required"
    if len(field) > 64:
        return "field name too long (>64)"
    if not _FIELD_NAME_RE.match(field):
        return "field name has invalid characters (alphanumeric + . _ only)"
    return None


def _validate_value(value: str | None, *, max_len: int, label: str) -> str | None:
    if value is None:
        return None
    if len(value) > max_len:
        return f"{label} too long (>{max_len})"
    return None


# ── Body builders ────────────────────────────────────────────────


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.isoformat()


def _build_dq_spot_check_queue_body(vm: DqSpotCheckQueueVM) -> object:
    """Pending-queue table for GET /dq/spot-check."""
    header = Tr(
        Th("Source"),
        Th("Drawn at"),
        Th("Record table"),
        Th("Record PK"),
        Th("Stratum"),
        Th(""),
    )
    body_rows = []
    if not vm.pending_rows:
        body_rows.append(
            Tr(Td("No pending samples — run `aslan-core audit spot-check-draw`.", colspan="6"))
        )
    for r in vm.pending_rows:
        body_rows.append(
            Tr(
                Th(r.source.upper()),
                Td(_fmt_dt(r.drawn_at)),
                Td(r.record_table),
                Td(r.record_pk_summary),
                Td(r.stratum or "—"),
                Td(A("Label", href=f"/dq/spot-check/{r.sample_id}")),
            )
        )
    return Div(
        H1("Data Quality — Spot Check"),
        P(
            f"{len(vm.pending_rows)} pending sample(s); "
            f"{vm.recent_completions} completed in the last 7 days.",
            _id="dq-spot-check-queue-summary",
        ),
        Table(header, *body_rows, cls="aslan-spot-check-queue"),
    )


def _build_dq_spot_check_detail_body(vm: DqSpotCheckSampleDetailVM) -> object:
    """Per-sample detail form for GET /dq/spot-check/<sample_id>."""
    pk_rows: list[object] = [
        Tr(Th("source"), Td(vm.source.upper())),
        Tr(Th("record_table"), Td(vm.record_table)),
        Tr(Th("drawn_at"), Td(_fmt_dt(vm.drawn_at))),
        Tr(Th("stratum"), Td(vm.stratum or "—")),
    ]
    for pair in vm.record_pk_pairs:
        pk_rows.append(Tr(Th(pair.key), Td(pair.value)))

    db_pane = Div(
        H2("DB row (canonical)"),
        Table(*pk_rows, cls="aslan-spot-check-pk"),
        cls="aslan-spot-check-pane aslan-spot-check-pane-db",
    )
    raw_pane = Div(
        H2("Raw bytes / API replay"),
        P(
            f"Status: {vm.raw_bytes_status}. Live raw-bytes pane lands in M2.1 — "
            "v1 surfaces only the canonical DB row above.",
            _id="dq-spot-check-raw-bytes-status",
        ),
        cls="aslan-spot-check-pane aslan-spot-check-pane-raw",
    )

    existing_rows: list[object] = [
        Tr(
            Th("Field"),
            Th("DB value"),
            Th("Truth value"),
            Th("Match"),
            Th("Variance %"),
            Th("Note"),
            Th("Labeller"),
            Th("At"),
        )
    ]
    for r in vm.existing_results:
        existing_rows.append(
            Tr(
                Td(r.field),
                Td(r.db_value or "—"),
                Td(r.truth_value or "—"),
                Td("✓" if r.matches else "✗"),
                Td("—" if r.variance_pct is None else f"{r.variance_pct:.4f}"),
                Td(r.label_note or "—"),
                Td(r.labeller),
                Td(_fmt_dt(r.recorded_at)),
            )
        )
    existing = Div(
        H2("Existing labels"),
        Table(*existing_rows, cls="aslan-spot-check-results"),
    )

    if vm.labelled:
        form: object = P(
            f"Sample labelled at {_fmt_dt(vm.labelled_at)} by {vm.labeller}. "
            "Append more results below if needed.",
            _id="dq-spot-check-already-labelled",
        )
    else:
        form = P("Pending — submit per-field truth values below.", _id="dq-spot-check-pending")

    label_form = Form(
        H2("Submit a label"),
        Label("Field name (e.g. revenue_try)", _for="field"),
        Input(type="text", name="field", id="field", required=True, maxlength="64"),
        Label("DB value (as observed)", _for="db_value"),
        Input(type="text", name="db_value", id="db_value", maxlength=str(_MAX_TRUTH_VALUE_LEN)),
        Label("Truth value (your reading from raw)", _for="truth_value"),
        Input(
            type="text",
            name="truth_value",
            id="truth_value",
            maxlength=str(_MAX_TRUTH_VALUE_LEN),
        ),
        Label("Note (optional)", _for="label_note"),
        Textarea(name="label_note", id="label_note", maxlength=str(_MAX_LABEL_NOTE_LEN), rows="3"),
        Label("Labeller", _for="labeller"),
        Input(type="text", name="labeller", id="labeller", required=True, maxlength="64"),
        Button("Submit", type="submit"),
        action=f"/dq/spot-check/{vm.sample_id}",
        method="post",
        cls="aslan-spot-check-form",
        _id="dq-spot-check-form",
    )

    corroborator_section = _build_corroborator_section(vm)

    return Div(
        H1(f"Spot Check — {vm.source.upper()} — {vm.sample_id}"),
        Span(_fmt_dt(vm.drawn_at), _id="dq-spot-check-drawn-at"),
        form,
        Div(db_pane, raw_pane, cls="aslan-spot-check-grid"),
        existing,
        label_form,
        corroborator_section,
    )


# ── Corroborator panel ───────────────────────────────────────────


def _cache_state_label(state: str) -> str:
    return {
        "fresh": "fresh",
        "stale": "stale (past 24-hour TTL)",
        "miss": "no cached fetch",
        "error": "last fetch failed",
        "unimplemented": "not yet implemented",
    }.get(state, state)


def _build_corroborator_panel(
    panel: DqSpotCheckCorroboratorPanelVM, *, sample_id: str
) -> object:
    """Render one corroborator source as a panel block.

    The block surfaces cache age, fetch latency, fetch status, the
    extracted payload key/value pairs, and a Refresh button (one POST
    target per source). For unimplemented sources the block renders a
    placeholder line and no Refresh button.
    """
    pieces: list[object] = [
        H2(f"Corroborator — {panel.source}"),
        P(
            f"State: {_cache_state_label(panel.cache_state)}. "
            f"Cache age: {panel.cached_age_label}.",
            _id=f"dq-corroborator-{panel.source}-state",
        ),
    ]
    if not panel.implemented:
        pieces.append(
            P(
                "This source is registered but not yet implemented. "
                "Adding a handler is documented in the NG6 handoff.",
                _id=f"dq-corroborator-{panel.source}-unimplemented",
            )
        )
        return Div(
            *pieces,
            cls=(
                "aslan-spot-check-corroborator "
                f"aslan-spot-check-corroborator-{panel.source} "
                "aslan-spot-check-corroborator-unimplemented"
            ),
            _id=f"dq-corroborator-{panel.source}",
        )

    if panel.fetch_url:
        pieces.append(
            P(
                A(
                    panel.fetch_url,
                    href=panel.fetch_url,
                    target="_blank",
                    rel="noopener noreferrer",
                ),
                _id=f"dq-corroborator-{panel.source}-url",
            )
        )

    if panel.fetch_status is not None and panel.fetch_latency_ms is not None:
        pieces.append(
            P(
                f"Last fetch: status={panel.fetch_status}, "
                f"latency={panel.fetch_latency_ms} ms.",
                _id=f"dq-corroborator-{panel.source}-fetch-meta",
            )
        )

    if panel.error_summary:
        pieces.append(
            P(
                f"Error: {panel.error_summary}",
                _id=f"dq-corroborator-{panel.source}-error",
            )
        )

    if panel.payload_pairs:
        payload_rows: list[object] = [Tr(Th("Field"), Th("Value"))]
        for pair in panel.payload_pairs:
            payload_rows.append(Tr(Th(pair.key), Td(pair.value)))
        pieces.append(
            Table(*payload_rows, cls="aslan-spot-check-corroborator-payload")
        )
    else:
        pieces.append(
            P(
                "No payload cached yet — click Refresh to fetch.",
                _id=f"dq-corroborator-{panel.source}-empty",
            )
        )

    refresh_form = Form(
        Button("Refresh", type="submit"),
        action=f"/dq/spot-check/{sample_id}/corroborator/{panel.source}/refresh",
        method="post",
        cls="aslan-spot-check-corroborator-refresh",
        _id=f"dq-corroborator-{panel.source}-refresh",
    )
    pieces.append(refresh_form)

    return Div(
        *pieces,
        cls=(
            "aslan-spot-check-corroborator "
            f"aslan-spot-check-corroborator-{panel.source}"
        ),
        _id=f"dq-corroborator-{panel.source}",
    )


def _build_corroborator_section(vm: DqSpotCheckSampleDetailVM) -> object:
    """Render the full corroborator section (one block per source).

    When ``vm.entity_ticker`` is empty the section renders a short
    "ticker unresolved" placeholder — the firecrawl call is not
    meaningful without a ticker, so the panel collapses gracefully.
    """
    if not vm.entity_ticker:
        return Div(
            H2("External corroborator"),
            P(
                "Could not resolve a BIST ticker for this sample. "
                "The corroborator panel surfaces a fresh second-opinion "
                "fetch keyed on the entity's BIST ticker.",
                _id="dq-spot-check-corroborator-no-ticker",
            ),
            cls="aslan-spot-check-corroborator-section",
            _id="dq-spot-check-corroborator-section",
        )
    pieces: list[object] = [
        H2("External corroborator"),
        P(
            f"Entity ticker: {vm.entity_ticker}. "
            f"{len(vm.corroborator_panels)} registered source(s).",
            _id="dq-spot-check-corroborator-summary",
        ),
    ]
    for panel in vm.corroborator_panels:
        pieces.append(
            _build_corroborator_panel(panel, sample_id=str(vm.sample_id))
        )
    return Div(
        *pieces,
        cls="aslan-spot-check-corroborator-section",
        _id="dq-spot-check-corroborator-section",
    )


register_template(DqSpotCheckQueueVM, _build_dq_spot_check_queue_body)
register_template(DqSpotCheckSampleDetailVM, _build_dq_spot_check_detail_body)


# ── Routes ───────────────────────────────────────────────────────


@app.get("/dq/spot-check")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_spot_check(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.dq_spot_check_queue(session)
    return render(request, vm)


@app.get("/dq/spot-check/{sample_id}")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_spot_check_detail(request: Request, sample_id: str) -> Response:
    try:
        sid = UUID(sample_id)
    except ValueError:
        return HTMLResponse(
            "<h1>Bad sample id</h1>",
            status_code=400,
        )
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.dq_spot_check_sample_detail(session, sample_id=sid)
    if vm is None:
        return HTMLResponse(
            "<h1>Sample not found</h1>",
            status_code=404,
        )
    return render(request, vm)


@app.post("/dq/spot-check/{sample_id}")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_spot_check_submit(request: Request, sample_id: str) -> Response:
    """Accept one label submission and 303-redirect back to the queue.

    Validates inputs against the bounded character classes / lengths
    declared at the top of this module — a bad field name or an
    over-length truth value returns 400 with a short error string.
    On success calls ``dq.spot_check.label_field()`` once and redirects
    back to ``/dq/spot-check`` so the labeller sees the updated queue.

    Privilege boundary: the dashboard role itself has SELECT-only on
    audit.spot_check_sample / audit.spot_check_result. Production
    deployment expects the labeller's session to authenticate as a
    write-capable role (``audit_writer`` holds the narrow column-level
    UPDATE plus INSERT). ``configure_app(...)`` is the dependency
    injection seam the deployment uses to bind the right factory.
    """
    try:
        sid = UUID(sample_id)
    except ValueError:
        return HTMLResponse("<h1>Bad sample id</h1>", status_code=400)

    form = await request.form()

    def _form_str(name: str) -> str | None:
        value = form.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            # Starlette's form() also returns UploadFile for multipart
            # file fields. We never accept files on this form.
            return None
        return value

    field = (_form_str("field") or "").strip()
    db_value_raw = _form_str("db_value")
    truth_value_raw = _form_str("truth_value")
    label_note_raw = _form_str("label_note")
    labeller = (_form_str("labeller") or "").strip()

    # Empty-string → None on the optional values so the audit row
    # carries SQL NULL rather than the empty string.
    db_value = db_value_raw.strip() if db_value_raw and db_value_raw.strip() else None
    truth_value = truth_value_raw.strip() if truth_value_raw and truth_value_raw.strip() else None
    label_note = label_note_raw.strip() if label_note_raw and label_note_raw.strip() else None

    err = _validate_field(field)
    if err is not None:
        return HTMLResponse(f"<h1>Bad input</h1><p>{err}</p>", status_code=400)
    if not labeller or len(labeller) > _MAX_LABELLER_LEN:
        return HTMLResponse(
            "<h1>Bad input</h1><p>labeller required, max 64 chars</p>",
            status_code=400,
        )
    err = _validate_value(db_value, max_len=_MAX_TRUTH_VALUE_LEN, label="db_value")
    if err is not None:
        return HTMLResponse(f"<h1>Bad input</h1><p>{err}</p>", status_code=400)
    err = _validate_value(truth_value, max_len=_MAX_TRUTH_VALUE_LEN, label="truth_value")
    if err is not None:
        return HTMLResponse(f"<h1>Bad input</h1><p>{err}</p>", status_code=400)
    err = _validate_value(label_note, max_len=_MAX_LABEL_NOTE_LEN, label="label_note")
    if err is not None:
        return HTMLResponse(f"<h1>Bad input</h1><p>{err}</p>", status_code=400)

    factory = get_session_factory()
    async with factory() as session:
        try:
            await spot_check.label_field(
                session=session,
                sample_id=sid,
                field=field,
                db_value=db_value,
                truth_value=truth_value,
                labeller=labeller,
                label_note=label_note,
            )
        except LookupError:
            return HTMLResponse("<h1>Sample not found</h1>", status_code=404)
        await session.commit()
    return RedirectResponse(url="/dq/spot-check", status_code=303)


@app.post("/dq/spot-check/{sample_id}/corroborator/{source}/refresh")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_spot_check_corroborator_refresh(
    request: Request, sample_id: str, source: str
) -> Response:
    """Force a fresh firecrawl fetch for one corroborator source.

    Validates inputs:
      * ``sample_id`` parses as UUID;
      * ``source`` matches the bounded character class and is in the
        registered-sources list (so a path-traversal-style value
        cannot smuggle past the lookup);
      * the sample exists and resolves to a non-empty entity_ticker
        (without a ticker, no fetch is meaningful).

    On success calls ``corroborator.refresh()`` (which writes a new
    cache row, possibly with ``fetch_status != 'ok'`` on a failed
    fetch) and 303-redirects back to the per-sample page so the
    panel renders the new state. The redirect is the htmx-friendly
    full-reload behaviour; per spec §6.3 the dashboard does not
    construct partial HTML responses.

    Privilege boundary: identical to the labelling POST — the
    dashboard role is SELECT-only on the cache table; production
    deployments route this through ``audit_writer``.
    """
    try:
        sid = UUID(sample_id)
    except ValueError:
        return HTMLResponse("<h1>Bad sample id</h1>", status_code=400)

    if not _CORROBORATOR_SOURCE_RE.match(source):
        return HTMLResponse("<h1>Bad source</h1>", status_code=400)
    if source not in corroborator.registered_sources():
        return HTMLResponse("<h1>Unknown corroborator source</h1>", status_code=404)
    if not corroborator.is_implemented(source):
        return HTMLResponse(
            "<h1>Corroborator source not yet implemented</h1>",
            status_code=400,
        )

    factory = get_session_factory()
    async with factory() as session:
        sample = await spot_check.get_sample(session=session, sample_id=sid)
        if sample is None:
            return HTMLResponse("<h1>Sample not found</h1>", status_code=404)
        entity_ticker = ""
        try:
            entity_ticker = await queries._entity_ticker_for_sample(
                session,
                source=sample.source,
                record_pk=sample.record_pk,
            )
        except Exception:
            entity_ticker = ""
        if not entity_ticker:
            return HTMLResponse(
                "<h1>Could not resolve entity ticker for this sample</h1>",
                status_code=400,
            )
        await corroborator.refresh(
            session=session, source=source, entity_ticker=entity_ticker
        )
        await session.commit()
    return RedirectResponse(url=f"/dq/spot-check/{sid}", status_code=303)
