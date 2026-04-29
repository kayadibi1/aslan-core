"""Public timeseries API for aslan-core v0.4.0.

The writer + reader operate on ``ts.series_catalog`` and
``ts.observation``. ORM models in ``aslan_core.models.ts`` are NOT
public — consumers go through this module.
"""

from __future__ import annotations

from aslan_core.timeseries.writer import ObservationWriter

__all__ = ["ObservationWriter"]
