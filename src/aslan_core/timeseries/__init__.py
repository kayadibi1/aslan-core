"""Public timeseries API for aslan-core v0.4.0.

The writer + reader operate on ``ts.series_catalog`` and
``ts.observation``. ORM models in ``aslan_core.models.ts`` are NOT
public — consumers go through this module.

PII helpers (codex F20) are re-exported so the Art. 17 deletion
runtime in ``aslan-service`` can share the same regex + walker as the
upsert-time guard in :class:`ObservationWriter`.
"""

from __future__ import annotations

from aslan_core.timeseries.pii import (
    PiiFinding,
    find_pii_in_clear_text,
    find_pii_in_metadata,
    find_pii_in_metadata_for_subject,
    has_numeric_string_keys,
    path_to_jsonb_set_text_array,
    scrub_in_python,
)
from aslan_core.timeseries.writer import ObservationWriter

__all__ = [
    "ObservationWriter",
    "PiiFinding",
    "find_pii_in_clear_text",
    "find_pii_in_metadata",
    "find_pii_in_metadata_for_subject",
    "has_numeric_string_keys",
    "path_to_jsonb_set_text_array",
    "scrub_in_python",
]
