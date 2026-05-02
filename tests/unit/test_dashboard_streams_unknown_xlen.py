"""Streams page renders ``?`` (not ``0``) for an unknown xlen.

Ultrareview bug_003: ``StreamRowVM.xlen`` was previously typed
``int`` and the page collapsed a failed Redis probe into ``0``,
which is operationally indistinguishable from a legitimate empty
stream. During a partial Redis incident an operator would see
``xlen=0`` for every degraded row and could wrongly conclude the
streams have been drained.

The fix lets ``xlen`` be ``None`` when the probe failed and renders
``?`` for that case, mirroring the existing ``last_entry_age_s:
float | None`` pattern (which already renders ``—`` on None). Tests
exercise both the empty-stream case (renders ``0``) and the
probe-failed case (renders ``?``).
"""

from __future__ import annotations

from aslan_core.dashboard.pages.streams import _build_streams_body
from aslan_core.dashboard.view_models import StreamRowVM, StreamsVM


def _row(*, stream_name: str, xlen: int | None) -> StreamRowVM:
    return StreamRowVM(
        stream_name=stream_name,
        xlen=xlen,
        last_entry_age_s=None,
        pending_per_group={},
    )


def _render_to_str(vm: StreamsVM) -> str:
    """Render the body builder's tree to HTML so the cell strings
    are visible. Avoids touching the FastHTML app or render() helper
    — this is a pure body-shape check."""
    from fasthtml.common import to_xml

    return str(to_xml(_build_streams_body(vm)))


def test_empty_stream_renders_xlen_as_zero() -> None:
    """A successfully-probed empty stream still renders ``0`` —
    the fix MUST NOT change the happy-path behavior."""
    vm = StreamsVM(
        rows=[_row(stream_name="aslan.kap.filings.new", xlen=0)],
        redis_circuit_open=False,
    )
    body = _render_to_str(vm)
    assert "<td>0</td>" in body


def test_failed_probe_renders_xlen_as_question_mark() -> None:
    """A failed probe (timeout / breaker open / budget exhausted)
    surfaces as ``?`` so an operator can tell it apart from an
    empty stream. ``xlen=None`` is the wire signal."""
    vm = StreamsVM(
        rows=[_row(stream_name="aslan.kap.filings.new", xlen=None)],
        redis_circuit_open=False,
    )
    body = _render_to_str(vm)
    assert "<td>?</td>" in body


def test_mixed_rows_render_correctly() -> None:
    """A page with one healthy probe + one failed probe MUST show
    each cell distinctly. The previous behavior would have produced
    ``<td>0</td>`` for both rows; the fix produces ``0`` and ``?``."""
    vm = StreamsVM(
        rows=[
            _row(stream_name="aslan.healthy", xlen=42),
            _row(stream_name="aslan.degraded", xlen=None),
            _row(stream_name="aslan.empty", xlen=0),
        ],
        redis_circuit_open=False,
    )
    body = _render_to_str(vm)
    assert "<td>42</td>" in body
    assert "<td>?</td>" in body
    assert "<td>0</td>" in body
