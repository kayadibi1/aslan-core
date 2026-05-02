"""Canonical stream-name constants for v0.5.0.

Every place in aslan-core that mentions a stream name imports from this
module — typos at call sites cannot drift from the allow-list. Codex
spec §9: the ``_KNOWN_STREAMS`` Prometheus allow-list mirrors this dict.

Public surface:

* :data:`STREAMS` — read-only ``MappingProxyType[str, str]`` of canonical
  stream names → human-readable descriptions.
* :data:`STREAM_FOR_EVENT_KIND` — read-only ``MappingProxyType[str, str]``
  of ``StreamEvent.kind`` → default stream name. Producer.publish uses
  this when no explicit ``stream=`` kwarg is passed.
* :data:`PII_BEARING_STREAMS` — frozenset of stream names whose payloads
  can structurally surface PII fields. The aslan-service Art. 17
  deletion runtime (Task 18 + a future aslan-service release) iterates
  over this set when scanning for events that need redaction.
* :func:`normalize_bist_ticks_label` — collapses per-symbol BIST streams
  (e.g., ``aslan.bist.ticks.AKBNK``) to the prefix ``aslan.bist.ticks``
  so the Prometheus ``stream`` label has bounded cardinality.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

_STREAMS: dict[str, str] = {
    "aslan.kap.filings.new": "KAP filing arrivals (initial publication)",
    "aslan.kap.filings.amended": "KAP filing amendments / revisions",
    "aslan.kap.filings.financial_report": "KAP financial-report-specific stream",
    "aslan.evds.observations.new": "EVDS macro-series observations",
    "aslan.tefas.observations.new": "TEFAS observations (NAV + holdings)",
    "aslan.tefas.nav.new": "TEFAS daily NAV publication",
    "aslan.bist.ticks": "BIST tick stream (per-symbol via prefix)",
    "aslan.entity.created": "New entity created via registry.put_entity",
}

STREAMS: Final[MappingProxyType[str, str]] = MappingProxyType(_STREAMS)
"""Frozen view of canonical stream names. Read-only; do NOT mutate."""


_STREAM_FOR_EVENT_KIND: dict[str, str] = {
    "filing.new": "aslan.kap.filings.new",
    "filing.amended": "aslan.kap.filings.amended",
    "observation.batch": "aslan.evds.observations.new",
    "entity.created": "aslan.entity.created",
    # NOTE: 'stream.entry_redacted' has no default mapping; the producer
    # MUST receive an explicit ``stream=`` kwarg pointing at the SAME
    # stream as the redacted target event.
}

STREAM_FOR_EVENT_KIND: Final[MappingProxyType[str, str]] = MappingProxyType(
    _STREAM_FOR_EVENT_KIND,
)


PII_BEARING_STREAMS: Final[frozenset[str]] = frozenset(
    {
        "aslan.kap.filings.new",
        "aslan.kap.filings.amended",
        "aslan.kap.filings.financial_report",
        # observation.batch can carry PII via metadata.subjects[]; flagged.
        "aslan.evds.observations.new",
        "aslan.tefas.observations.new",
        # entity.created carries legal_name (organization-shaped, but
        # an individual issuer's name structurally surfaces).
        "aslan.entity.created",
    }
)
"""Streams whose Pydantic payloads can structurally carry PII fields.

The aslan-service Art. 17 deletion runtime (Task 18 + a future
aslan-service release) iterates over this set when scanning for events
that need redaction.
"""


def normalize_bist_ticks_label(stream: str) -> str:
    """Collapse per-symbol BIST streams to the bounded ``aslan.bist.ticks`` label.

    Codex spec §9 — applied by the producer + consumer + drainer at
    every metric increment site so the Prometheus ``stream`` label has
    bounded cardinality.

    :param stream: A canonical stream name, possibly with a per-symbol
        suffix (e.g., ``aslan.bist.ticks.AKBNK``).
    :returns: The stream name with any BIST per-symbol suffix collapsed
        to the prefix ``aslan.bist.ticks``. Non-BIST streams pass
        through unchanged.
    """
    if stream.startswith("aslan.bist.ticks.") and stream != "aslan.bist.ticks":
        return "aslan.bist.ticks"
    return stream


__all__ = [
    "PII_BEARING_STREAMS",
    "STREAMS",
    "STREAM_FOR_EVENT_KIND",
    "normalize_bist_ticks_label",
]
