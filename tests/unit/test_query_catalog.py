"""Unit tests for ``aslan_core.query.catalog``.

Covers:
- load_canonical_lines returns a non-empty list from a fixture YAML
- list_canonical_lines with StatementType.IS returns only IS lines
- list_canonical_lines with StatementType.BS returns only BS lines
- list_canonical_lines with no filter returns all lines
- Every loaded item has all required fields populated
- load_canonical_lines raises FileNotFoundError for a missing file
- load_canonical_lines raises ValueError when the 'lines' key is absent
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aslan_core.query.catalog import list_canonical_lines, load_canonical_lines
from aslan_core.query.schemas import CanonicalLineInfo, StatementType

# ---------------------------------------------------------------------------
# Fixture path
# ---------------------------------------------------------------------------

_FIXTURE = Path(__file__).parent / "fixtures" / "canonical_lines_test.yaml"


# ---------------------------------------------------------------------------
# load_canonical_lines
# ---------------------------------------------------------------------------


def test_load_returns_nonempty_list() -> None:
    lines = load_canonical_lines(_FIXTURE)
    assert len(lines) > 0


def test_load_returns_canonical_line_info_instances() -> None:
    lines = load_canonical_lines(_FIXTURE)
    for line in lines:
        assert isinstance(line, CanonicalLineInfo)


def test_load_all_required_fields_present() -> None:
    """Every item must have all six required fields populated."""
    lines = load_canonical_lines(_FIXTURE)
    for line in lines:
        assert line.canonical_code
        assert line.statement_type in StatementType
        assert line.description
        assert isinstance(line.computed, bool)
        assert isinstance(line.required, bool)
        assert isinstance(line.monetary, bool)


def test_load_raises_for_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        load_canonical_lines(Path("/nonexistent/canonical_lines.yaml"))


def test_load_raises_for_missing_lines_key(tmp_path: Path) -> None:
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("manifest_version: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no 'lines' key"):
        load_canonical_lines(bad_yaml)


# ---------------------------------------------------------------------------
# list_canonical_lines — filtering
# ---------------------------------------------------------------------------


def test_no_filter_returns_all() -> None:
    lines = load_canonical_lines(_FIXTURE)
    result = list_canonical_lines(lines)
    assert result == lines


def test_no_filter_returns_copy_not_same_object() -> None:
    lines = load_canonical_lines(_FIXTURE)
    result = list_canonical_lines(lines)
    assert result is not lines


def test_filter_by_is_returns_only_is_lines() -> None:
    lines = load_canonical_lines(_FIXTURE)
    result = list_canonical_lines(lines, statement_type=StatementType.IS)
    assert len(result) > 0
    assert all(li.statement_type == StatementType.IS for li in result)


def test_filter_by_bs_returns_only_bs_lines() -> None:
    lines = load_canonical_lines(_FIXTURE)
    result = list_canonical_lines(lines, statement_type=StatementType.BS)
    assert len(result) > 0
    assert all(li.statement_type == StatementType.BS for li in result)


def test_filter_by_cf_returns_only_cf_lines() -> None:
    lines = load_canonical_lines(_FIXTURE)
    result = list_canonical_lines(lines, statement_type=StatementType.CF)
    assert len(result) > 0
    assert all(li.statement_type == StatementType.CF for li in result)


def test_filter_counts_add_up_to_total() -> None:
    lines = load_canonical_lines(_FIXTURE)
    is_count = len(list_canonical_lines(lines, statement_type=StatementType.IS))
    bs_count = len(list_canonical_lines(lines, statement_type=StatementType.BS))
    cf_count = len(list_canonical_lines(lines, statement_type=StatementType.CF))
    assert is_count + bs_count + cf_count == len(lines)


def test_filter_is_excludes_non_is() -> None:
    lines = load_canonical_lines(_FIXTURE)
    result = list_canonical_lines(lines, statement_type=StatementType.IS)
    assert not any(li.statement_type != StatementType.IS for li in result)


def test_empty_input_returns_empty() -> None:
    result = list_canonical_lines([], statement_type=StatementType.IS)
    assert result == []


def test_empty_input_no_filter_returns_empty() -> None:
    result = list_canonical_lines([])
    assert result == []
