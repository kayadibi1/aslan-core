from __future__ import annotations

from typing import Any

from aslan_core.streams.pii import PII_BEARING_STREAMS, redact_outbox_payload


def test_redact_outbox_payload_replaces_dotted_paths_without_mutating_input() -> None:
    payload: dict[str, Any] = {
        "title": "Jane Doe",
        "metadata": {"subjects": [{"subject_id": "person-1"}]},
    }

    redacted = redact_outbox_payload(
        payload,
        ["title", "metadata.subjects.0.subject_id"],
    )

    assert redacted["title"] == "<redacted>"
    assert redacted["metadata"]["subjects"][0]["subject_id"] == "<redacted>"
    assert payload["title"] == "Jane Doe"
    assert payload["metadata"]["subjects"][0]["subject_id"] == "person-1"


def test_pii_bearing_streams_reexport() -> None:
    assert "aslan.kap.filings.new" in PII_BEARING_STREAMS
