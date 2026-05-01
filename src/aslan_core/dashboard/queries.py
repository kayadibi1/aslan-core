"""Static-SQL query helpers. Filled in Task 5 of the v0.6.0 plan.

Every helper returns a view-model from ``view_models.py``. Per spec
§6.3 + §8.1, this is the ONLY module in ``aslan_core.dashboard`` that
imports ``sqlalchemy.text`` — the AST scan in
``test_dashboard_queries_have_static_sql_only.py`` enforces that
constraint at lint time.
"""

from __future__ import annotations
