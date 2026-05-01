"""Spec §8 test names resolve to files under ``tests/``.

Codex plan-round-1: every ``test_dashboard_*.py`` named in the
v0.6.0 spec must exist under ``tests/`` somewhere — otherwise the
plan's contract sweep landed without one of the named tests.

The check parses the spec markdown, extracts every
``test_dashboard_*.py`` mention, and asserts each resolves to a
file in the test tree. A future spec change that names a new
test trips this meta-test with a missing-file message,
prompting the engineer to write the test before merging.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SPEC_PATH = Path("/Users/sidaraslanoglu/Desktop/crawl/plans/aslan-core-v0.6.0-dashboard.md")
_TESTS_ROOT = Path(__file__).parent.parent

# Spec mentions a few names that aren't actually planned as tests
# (referenced in prose, e.g. "see test_dashboard_xxx.py for ..."
# pointing at a contract). Tests already covered under different
# filenames OR explicitly out-of-scope land here.
_KNOWN_SPEC_ONLY: frozenset[str] = frozenset(
    {
        # codex F-3 follow-up — v0.6.0 plan-round dropped the helper.
        # Spec text still references it, so we explicitly accept the absence.
        "test_dashboard_outbox_payload_size_helper.py",
        # Per-page sentinel test names consolidated into
        # tests/integration/test_dashboard_sentinel_matrix.py — Task 13.
        "test_dashboard_outbox_no_payload_leak.py",
        "test_dashboard_outbox_no_last_error_leak.py",
        "test_dashboard_deadletter_no_payload_excerpt_leak.py",
        "test_dashboard_deadletter_no_last_error_leak.py",
        "test_dashboard_ingestion_no_last_error_leak.py",
        "test_dashboard_documents_no_body_text_leak.py",
        "test_dashboard_documents_no_attachment_blob_leak.py",
        "test_dashboard_redactions_no_redacted_payload_leak.py",
        "test_dashboard_audit_no_metadata_leak.py",
        "test_dashboard_audit_truncates_client_ip.py",
        # Per-page banner test names consolidated into
        # tests/integration/test_dashboard_compliance_banner.py — Task 9.
        "test_dashboard_audit_page_renders_compliance_banner.py",
        "test_dashboard_redactions_page_renders_compliance_banner.py",
        # DML at privilege layer covered by
        # test_dashboard_hard_boundary_survives_guc_override.py and
        # test_dashboard_forbidden_columns_are_revoked.py.
        "test_dashboard_dml_blocked_at_privilege_layer.py",
        # FOR UPDATE at GUC layer covered by
        # test_dashboard_soft_boundary_blocked_only_by_guc.py.
        "test_dashboard_for_update_blocked_at_guc_layer.py",
    }
)


def _spec_test_names() -> set[str]:
    if not _SPEC_PATH.exists():
        pytest.skip(f"spec markdown not present at {_SPEC_PATH}")
    text = _SPEC_PATH.read_text(encoding="utf-8")
    pattern = re.compile(r"test_dashboard_[a-zA-Z0-9_]+\.py")
    return set(pattern.findall(text))


def test_every_spec_named_test_has_a_file() -> None:
    expected = _spec_test_names() - _KNOWN_SPEC_ONLY
    if not expected:
        pytest.skip("no test_dashboard_*.py names found in spec")

    existing = {p.name for p in _TESTS_ROOT.rglob("test_dashboard_*.py")}
    missing = sorted(expected - existing)
    assert not missing, (
        f"spec §8 references these tests, but no file exists under tests/: "
        f"{missing!r}. Either land the test or explicitly add the name to "
        "_KNOWN_SPEC_ONLY in this file (with a one-line reason)."
    )
