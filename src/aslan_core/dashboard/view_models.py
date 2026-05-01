"""Deny-by-default Pydantic view models. Filled in Task 4 of the v0.6.0 plan.

Every model uses ``ConfigDict(extra='forbid', frozen=True)`` so a future
query that selects a forbidden column cannot accidentally flow into a
template via an unexpected key. The structural-floor test in
``test_dashboard_view_models.py`` enforces ``extra='forbid'``;
``test_dashboard_vm_field_types_are_safe.py`` enforces the type
allowlist (no ``Any`` / ``object`` / SQLAlchemy ``Row``).
"""

from __future__ import annotations
