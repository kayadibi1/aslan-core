"""Per-source recency probes — public surface for `audit recency-sweep`.

Each probe answers two questions for one (source, dimension) pair:

  * `upstream_latest(session, dimension)` — what is the freshest
    record the source publisher has? "Now" minus a small offset for
    push-based sources (KAP), the most recent expected calendar slot
    for scheduled sources (EVDS), or the last business-day close for
    market sources (BIST). The actual implementations for these are
    `# TODO(M1.1)` placeholders for v1; the real upstream queries
    land in the next session.

  * `db_latest(session, dimension)` — what is the freshest record we
    have ingested from this source? Reads `MAX(<source-specific
    timestamp>)` from the source's primary table. Returns `None` if
    the source's table is not present (puller not yet deployed) so
    the cron can record "source not deployed" instead of failing.

The two timestamps are then differenced to compute lag against the
SLA target seeded in `audit.recency_sla` (migration 0054). The cron
inserts one row per (source, dimension) per sweep into
`audit.recency_observation`; rows where `sla_breached=true` also
emit an `audit.event` of type `recency_sla_breach`.

Probe instances are stateless; the cron constructs one per source
and reuses it across dimensions.
"""

from __future__ import annotations

from aslan_core.dq.probes._protocol import Probe, ProbeResult
from aslan_core.dq.probes.bist import BistProbe
from aslan_core.dq.probes.evds import EvdsProbe
from aslan_core.dq.probes.kap import KapProbe
from aslan_core.dq.probes.mkk import MkkProbe
from aslan_core.dq.probes.tefas import TefasProbe

__all__ = [
    "BistProbe",
    "EvdsProbe",
    "KapProbe",
    "MkkProbe",
    "Probe",
    "ProbeResult",
    "TefasProbe",
]


# Canonical source → probe constructor map. The CLI iterates this
# in deterministic order (`SOURCES` tuple).
SOURCES: tuple[str, ...] = ("kap", "evds", "bist", "tefas", "mkk")


def get_probe(source: str) -> Probe:
    """Return the canonical probe instance for `source`.

    Raises `ValueError` if the source is not one of the five known
    sources. The CLI passes only seeded sources from
    `audit.recency_sla`, so a miss here indicates a seed/code drift.
    """
    match source:
        case "kap":
            return KapProbe()
        case "evds":
            return EvdsProbe()
        case "bist":
            return BistProbe()
        case "tefas":
            return TefasProbe()
        case "mkk":
            return MkkProbe()
        case _:
            raise ValueError(f"unknown source: {source!r}")
