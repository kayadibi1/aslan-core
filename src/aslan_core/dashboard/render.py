"""Render helper + base template. Filled in Task 7 of the v0.6.0 plan.

Every page handler returns ``render(request, ...)`` (or its tested
wrappers). The unit test
``test_dashboard_render_helper_only_path.py`` enforces this — a handler
that returns a fragment dict directly fails the test, closing the
htmx-bypass-the-VM hole.
"""

from __future__ import annotations
