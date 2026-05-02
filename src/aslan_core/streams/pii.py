"""PII redaction helpers for stream event payloads."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from aslan_core.streams.events import StreamEntryRedactedEvent
from aslan_core.streams.names import PII_BEARING_STREAMS


def redact_outbox_payload(
    payload: dict[str, Any],
    fields_to_redact: list[str],
) -> dict[str, Any]:
    """Return a deep-copied payload with dotted paths replaced."""
    out = deepcopy(payload)
    for path in fields_to_redact:
        _redact_path(out, path.split("."))
    return out


def _redact_path(node: Any, parts: list[str]) -> None:
    if not parts:
        return
    head, *rest = parts
    try:
        idx: int | str = int(head)
    except ValueError:
        idx = head

    if rest:
        if _contains_path_part(node, idx):
            _redact_path(node[idx], rest)
        return

    if _contains_path_part(node, idx):
        node[idx] = "<redacted>"


def _contains_path_part(node: Any, idx: int | str) -> bool:
    return (isinstance(node, dict) and idx in node) or (
        isinstance(node, list) and isinstance(idx, int) and 0 <= idx < len(node)
    )


__all__ = [
    "PII_BEARING_STREAMS",
    "StreamEntryRedactedEvent",
    "redact_outbox_payload",
]
