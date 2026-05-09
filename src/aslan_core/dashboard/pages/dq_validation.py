"""/dq/validation — validation-failure browser + regression review.

M5 deliverable per spec §10.7 + §7.4 + §11.

Three GET sections on the page:

  * **Top** — per-rule failure rate over the trailing 7 days, sourced
    from ``audit.validation_failure``. The rule_name column groups
    in-line per-record validators (``ts_canonical_financial.*``) and
    cross-source rules (``xs_*``) into one table — they all write to
    the same audit table and a single failure-rate trend is more
    operationally useful than splitting the tables.

  * **Middle** — open ``audit.regression_flag`` rows (``status='open'``)
    with click-through review actions: dismiss / confirm bug / mark
    reviewed. Status mutations write
    ``audit.event(event_type='regression_flag_reviewed')`` so every
    transition is audited; v2 auto-dismissals are visible here too
    (the cron writes the same event with ``review_note``
    starting ``"auto-dismissed: justified by KAP filing …"``).

  * **Bottom** — recent cross-source rule skip events
    (``event_type='xs_rule_skipped'``) so operators can see when a
    cron run skipped a rule because its source tables are absent.

One POST route:

  * ``POST /dq/validation/regression/{flag_id}`` — review action.
    Validates the form input and calls ``dq.regression.set_status`` with
    status ∈ {reviewed, dismissed, confirmed_bug}. 303-redirects back
    to ``/dq/validation`` on success.

Privilege boundary: dashboard role has SELECT-only on
``audit.regression_flag`` (migration 0061). Production deployment
expects the review POST to authenticate against a write-capable role
(``audit_admin`` holds the column-level UPDATE GRANT). The
``configure_app(...)`` dependency-injection seam binds the right
factory per the spot-check pattern.
"""

from __future__ import annotations

from datetime import datetime

from fasthtml.common import (
    H1,
    H2,
    H3,
    Button,
    Div,
    Form,
    Input,
    P,
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
from aslan_core.dashboard.view_models import DqValidationVM
from aslan_core.dq import regression

# ── Input validation constants ────────────────────────────────────


_VALID_REVIEW_STATUSES: frozenset[str] = frozenset({"reviewed", "dismissed", "confirmed_bug"})
_MAX_REVIEWER_LEN = 64
_MAX_REVIEW_NOTE_LEN = 2048


# ── Body builder ─────────────────────────────────────────────────


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.isoformat()


def _build_dq_validation_body(vm: DqValidationVM) -> object:
    """Three-section page body for GET /dq/validation."""
    rule_header = Tr(
        Th("Rule"),
        Th("Source"),
        Th("Failures (7d)"),
        Th("Last failure at"),
    )
    rule_body: list[object] = []
    if not vm.rule_rows:
        rule_body.append(
            Tr(
                Td(
                    "No validation failures in the last 7 days. "
                    "Run `aslan-core audit cross-source-consistency` to populate.",
                    colspan="4",
                )
            )
        )
    for r in vm.rule_rows:
        rule_body.append(
            Tr(
                Th(r.rule_name),
                Td(r.source.upper()),
                Td(str(r.failures_7d)),
                Td(_fmt_dt(r.last_failure_at)),
            )
        )

    rule_section = Div(
        H2("Validation rule failures (last 7 days)"),
        Table(rule_header, *rule_body, cls="aslan-validation-rules"),
        _id="dq-validation-rules",
    )

    flag_header = Tr(
        Th("Flag id"),
        Th("Source"),
        Th("Metric"),
        Th("Prior"),
        Th("Current"),
        Th("Shift %"),
        Th("Threshold %"),
        Th("Detected at"),
        Th("Review"),
    )
    flag_body: list[object] = []
    if not vm.regression_rows:
        flag_body.append(
            Tr(
                Td(
                    "No open regression flags. Run "
                    "`aslan-core audit regression-detect` to populate.",
                    colspan="9",
                )
            )
        )
    for f in vm.regression_rows:
        review_form = Form(
            Input(type="hidden", name="status", value="reviewed"),
            Input(
                type="text",
                name="reviewer",
                placeholder="reviewer (e.g. sidar)",
                required=True,
                maxlength=str(_MAX_REVIEWER_LEN),
            ),
            Textarea(
                name="review_note",
                placeholder="optional note",
                maxlength=str(_MAX_REVIEW_NOTE_LEN),
                rows="1",
            ),
            Button("Mark reviewed", type="submit", name="status", value="reviewed"),
            Button("Dismiss", type="submit", name="status", value="dismissed"),
            Button(
                "Confirm bug",
                type="submit",
                name="status",
                value="confirmed_bug",
            ),
            action=f"/dq/validation/regression/{f.flag_id}",
            method="post",
            cls="aslan-validation-review-form",
            _id=f"dq-validation-review-form-{f.flag_id}",
        )
        flag_body.append(
            Tr(
                Th(str(f.flag_id)),
                Td(f.source.upper()),
                Td(f.metric),
                Td(f.prior_value or "—"),
                Td(f.current_value or "—"),
                Td(f.shift_pct),
                Td(f.threshold_pct),
                Td(_fmt_dt(f.detected_at)),
                Td(review_form),
            )
        )

    flag_section = Div(
        H2("Open regression flags"),
        P(
            f"{len(vm.regression_rows)} open flag(s). v2 auto-dismissed flags "
            "appear in `audit.event(event_type='regression_auto_dismissed')` "
            "and are NOT shown here — they leave `status='dismissed'`.",
            _id="dq-validation-flags-summary",
        ),
        Table(flag_header, *flag_body, cls="aslan-validation-flags"),
        _id="dq-validation-regression-flags",
    )

    skip_header = Tr(
        Th("Rule"),
        Th("Reason"),
        Th("Missing"),
        Th("Emitted at"),
    )
    skip_body: list[object] = []
    if not vm.xs_skip_rows:
        skip_body.append(
            Tr(
                Td(
                    "No xs_rule_skipped events in the last 7 days.",
                    colspan="4",
                )
            )
        )
    for s in vm.xs_skip_rows:
        skip_body.append(
            Tr(
                Th(s.rule_name),
                Td(s.reason),
                Td(s.missing_summary),
                Td(_fmt_dt(s.emitted_at)),
            )
        )

    skip_section = Div(
        H2("Cross-source rule skips (last 7 days)"),
        P(
            "A skip means the rule's required source tables aren't present "
            "on this branch / environment. Re-runs are no-ops until the "
            "tables land.",
            _id="dq-validation-skips-summary",
        ),
        Table(skip_header, *skip_body, cls="aslan-validation-skips"),
        _id="dq-validation-xs-skips",
    )

    return Div(
        H1("Data Quality — Validation"),
        H3(
            f"{sum(r.failures_7d for r in vm.rule_rows)} failures across "
            f"{len(vm.rule_rows)} rule(s); "
            f"{len(vm.regression_rows)} open regression flag(s); "
            f"{len(vm.xs_skip_rows)} skip event(s).",
            _id="dq-validation-summary",
        ),
        rule_section,
        flag_section,
        skip_section,
    )


register_template(DqValidationVM, _build_dq_validation_body)


# ── Routes ───────────────────────────────────────────────────────


@app.get("/dq/validation")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_validation(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.dq_validation(session)
    return render(request, vm)


@app.post("/dq/validation/regression/{flag_id}")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def dq_validation_regression_review(request: Request, flag_id: str) -> Response:
    """Accept one regression-flag review action and 303-redirect to /dq/validation.

    Form fields:
      * ``status`` (required) — one of {reviewed, dismissed, confirmed_bug}.
      * ``reviewer`` (required) — labeller identity, max 64 chars.
      * ``review_note`` (optional) — max 2048 chars.

    Validation errors return 400 with a short error string; an unknown
    flag_id returns 404. On success, ``dq.regression.set_status`` is
    called and an ``audit.event(event_type='regression_flag_reviewed')``
    is emitted — both happen inside one transaction.
    """
    try:
        fid = int(flag_id)
    except ValueError:
        return HTMLResponse("<h1>Bad flag id</h1>", status_code=400)
    if fid <= 0:
        return HTMLResponse("<h1>Bad flag id</h1>", status_code=400)

    form = await request.form()

    def _form_str(name: str) -> str | None:
        value = form.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        return value

    status = (_form_str("status") or "").strip()
    reviewer = (_form_str("reviewer") or "").strip()
    review_note_raw = _form_str("review_note")
    review_note = review_note_raw.strip() if review_note_raw and review_note_raw.strip() else None

    if status not in _VALID_REVIEW_STATUSES:
        return HTMLResponse(
            f"<h1>Bad input</h1><p>status must be one of "
            f"{sorted(_VALID_REVIEW_STATUSES)}; got {status!r}</p>",
            status_code=400,
        )
    if not reviewer or len(reviewer) > _MAX_REVIEWER_LEN:
        return HTMLResponse(
            "<h1>Bad input</h1><p>reviewer required, max 64 chars</p>",
            status_code=400,
        )
    if review_note is not None and len(review_note) > _MAX_REVIEW_NOTE_LEN:
        return HTMLResponse(
            "<h1>Bad input</h1><p>review_note too long (>2048)</p>",
            status_code=400,
        )

    factory = get_session_factory()
    async with factory() as session:
        try:
            await regression.set_status(
                session=session,
                flag_id=fid,
                status=status,
                reviewer=reviewer,
                review_note=review_note,
            )
        except LookupError:
            return HTMLResponse("<h1>Flag not found</h1>", status_code=404)
        await session.commit()
    return RedirectResponse(url="/dq/validation", status_code=303)
