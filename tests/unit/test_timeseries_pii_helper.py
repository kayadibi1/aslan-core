"""Unit tests — ``aslan_core.timeseries.pii``.

Plan reference: v0.4.0 Task 11. Covers codex F4 (clear-text scan),
F19 (metadata.fields scanned recursively), F20 (public helper for
aslan-service backstop), F21 (subject-aware filter + span scrub
contract), F22 (multi-match), F23 (structured path tuple →
``jsonb_set`` text[]), F24 (numeric-string-key validator), F26
(``has_numeric_string_keys`` + ``scrub_in_python`` bypass-fallback).
"""

from __future__ import annotations

from typing import Any

import pytest

from aslan_core.timeseries.pii import (
    PiiFinding,
    find_pii_in_clear_text,
    find_pii_in_metadata,
    find_pii_in_metadata_for_subject,
    path_to_jsonb_set_text_array,
)


def test_find_pii_in_clear_text_email() -> None:
    findings = find_pii_in_clear_text("series_code", "exec.john.doe@aselsan.com")
    assert any(f.matched_pattern == "email" for f in findings)


def test_find_pii_in_clear_text_name() -> None:
    findings = find_pii_in_clear_text("description", "compensation for John Doe")
    assert any(f.matched_pattern == "name" for f in findings)


def test_find_pii_in_clear_text_clean() -> None:
    assert find_pii_in_clear_text("series_code", "exec.compensation.opaque") == []


def test_find_pii_in_metadata_skips_subjects_top_level() -> None:
    # Subject-channel email: the structured channel; not a finding.
    md = {"subjects": [{"subject_id": "person:abc", "role": "executive"}]}
    assert find_pii_in_metadata(md) == []


def test_find_pii_in_metadata_under_fields_email() -> None:
    md = {"fields": {"email": "leak@example.com"}}
    findings = find_pii_in_metadata(md)
    assert len(findings) == 1
    assert findings[0].path == ("fields", "email")
    assert findings[0].json_path == "$.fields.email"
    assert findings[0].matched_pattern == "email"


def test_find_pii_in_metadata_recursive_under_fields() -> None:
    md = {"fields": {"nested": {"contact": "john.doe@example.com"}}}
    findings = find_pii_in_metadata(md)
    assert len(findings) == 1
    assert findings[0].path == ("fields", "nested", "contact")
    assert findings[0].json_path == "$.fields.nested.contact"


def test_find_pii_in_metadata_recursive_in_list() -> None:
    md = {"fields": {"contacts": ["a@b.c", "clean string"]}}
    findings = find_pii_in_metadata(md)
    assert len(findings) == 1
    assert findings[0].path == ("fields", "contacts", 0)
    assert findings[0].json_path == "$.fields.contacts[0]"


def test_find_pii_in_metadata_under_arbitrary_top_level_key() -> None:
    # codex F20 bypass scenario: a future operator UPDATE writes
    # metadata.notes = '...email...' directly, skipping the writer.
    # The helper MUST still find it.
    md = {"notes": "contact a@b.com for details"}
    findings = find_pii_in_metadata(md)
    assert len(findings) == 1
    assert findings[0].path == ("notes",)
    assert findings[0].json_path == "$.notes"


def test_find_pii_in_metadata_clean_fields_no_findings() -> None:
    md = {"fields": {"compensation": 1_000_000, "role_label": "CEO", "period": "FY2025"}}
    assert find_pii_in_metadata(md) == []


def test_find_pii_in_metadata_returns_all_findings_not_just_first() -> None:
    md = {"fields": {"a": "x@y.z", "b": "John Smith"}}
    findings = find_pii_in_metadata(md)
    paths = {f.json_path for f in findings}
    assert "$.fields.a" in paths
    assert "$.fields.b" in paths


def test_find_pii_in_metadata_empty_or_none() -> None:
    assert find_pii_in_metadata(None) == []
    assert find_pii_in_metadata({}) == []


# ----- Codex F21 — span-based, subject-aware contract -----


def test_pii_finding_includes_matched_text_and_span() -> None:
    """Codex F21: PiiFinding carries the EXACT substring + (start, end)
    indices into full_value so the deletion runtime can splice-replace
    without re-parsing."""
    md = {"notes": "contact john.doe@example.com for details"}
    findings = find_pii_in_metadata(md)
    assert len(findings) == 1
    f = findings[0]
    assert f.matched_text == "john.doe@example.com"
    assert f.full_value == "contact john.doe@example.com for details"
    start, end = f.span
    assert f.full_value[start:end] == f.matched_text


def test_find_pii_in_metadata_for_subject_filters_to_target_only() -> None:
    """Codex F21: metadata contains email for target subject AND email
    for an unrelated person; subject-aware finder returns ONLY the
    target's email. Non-target PII is left alone (Art. 17 does not
    extend to other data subjects)."""
    md = {
        "fields": {
            "target_email": "john.doe@example.com",
            "unrelated_email": "jane.smith@somewhereelse.com",
        }
    }
    findings = find_pii_in_metadata_for_subject(
        md, subject_identifiers=frozenset({"john.doe@example.com"})
    )
    assert len(findings) == 1
    assert findings[0].matched_text == "john.doe@example.com"
    assert findings[0].json_path == "$.fields.target_email"


def test_find_pii_in_metadata_for_subject_empty_identifiers_returns_empty() -> None:
    md = {"fields": {"email": "a@b.c"}}
    assert find_pii_in_metadata_for_subject(md, subject_identifiers=frozenset()) == []


def test_subject_aware_finder_handles_nfc_aliases() -> None:
    """Codex F21: subject_identifiers contains an NFD-normalized name;
    metadata contains the NFC-normalized version; finder still matches.
    Uses Turkish char ``İ`` which has distinct NFC/NFD forms."""
    import unicodedata

    nfc_name = unicodedata.normalize("NFC", "İrem Yıldız")
    nfd_alias = unicodedata.normalize("NFD", "İrem Yıldız")
    md = {"fields": {"contact": f"reach out to {nfc_name} directly"}}
    findings = find_pii_in_metadata_for_subject(md, subject_identifiers=frozenset({nfd_alias}))
    assert len(findings) == 1
    # Sanity: the same identifier set matches case-insensitively too.
    findings_lower = find_pii_in_metadata_for_subject(
        md, subject_identifiers=frozenset({nfd_alias.lower()})
    )
    assert len(findings_lower) == 1


def test_subject_aware_finder_case_insensitive() -> None:
    md = {"fields": {"email": "John.Doe@Example.COM"}}
    findings = find_pii_in_metadata_for_subject(
        md, subject_identifiers=frozenset({"john.doe@example.com"})
    )
    assert len(findings) == 1


# ----- Codex F22 — multi-match-per-string contract -----


def test_multiple_emails_in_one_string_each_emits_finding() -> None:
    """Codex F22: a single string with two emails MUST produce two
    findings."""
    md = {"fields": {"contact": "Contact alice@a.com or bob@b.com for help"}}
    findings = find_pii_in_metadata(md)
    emails = sorted(f.matched_text for f in findings if f.matched_pattern == "email")
    assert emails == ["alice@a.com", "bob@b.com"]
    spans = sorted(f.span for f in findings if f.matched_pattern == "email")
    assert len(set(spans)) == 2
    for f in findings:
        if f.matched_pattern == "email":
            start, end = f.span
            assert f.full_value[start:end] == f.matched_text


def test_target_after_non_target_is_caught() -> None:
    """Codex F22 + F21: the target subject's email appears AFTER an
    unrelated email in the same string. Subject-aware finder returns
    ONLY the target's finding."""
    md = {"fields": {"contact": "Contact alice@a.com or bob@b.com"}}
    findings = find_pii_in_metadata_for_subject(md, subject_identifiers=frozenset({"bob@b.com"}))
    assert len(findings) == 1
    f = findings[0]
    assert f.matched_text == "bob@b.com"
    start, end = f.span
    assert f.full_value[start:end] == "bob@b.com"


def test_target_before_non_target_is_caught() -> None:
    """Codex F22 + F21: same as above with the order reversed — both
    orderings must work."""
    md = {"fields": {"contact": "Contact bob@b.com or alice@a.com"}}
    findings = find_pii_in_metadata_for_subject(md, subject_identifiers=frozenset({"bob@b.com"}))
    assert len(findings) == 1
    f = findings[0]
    assert f.matched_text == "bob@b.com"
    start, end = f.span
    assert f.full_value[start:end] == "bob@b.com"


def test_multiple_findings_in_same_path_replaced_in_reverse_span_order() -> None:
    """Codex F22: simulate the deletion-runtime scrub loop manually.
    Apply replacements to the SAME `full_value` in REVERSE span order
    so earlier (left) replacements don't shift later (right) span
    indices."""
    md = {"fields": {"contact": "Contact alice@a.com or bob@b.com for help"}}
    findings = find_pii_in_metadata_for_subject(md, subject_identifiers=frozenset({"bob@b.com"}))
    assert len(findings) == 1
    full_value = findings[0].full_value
    for f in sorted(findings, key=lambda f: f.span, reverse=True):
        full_value = (
            full_value[: f.span[0]]
            + f"[REDACTED:Art.17:{f.matched_pattern}]"
            + full_value[f.span[1] :]
        )
    assert "alice@a.com" in full_value  # non-target preserved
    assert "bob@b.com" not in full_value  # target scrubbed
    assert "[REDACTED:Art.17:email]" in full_value


def test_two_targets_in_same_string_replaced_in_reverse_span_order() -> None:
    """Codex F22: when BOTH matches in a string are target-PII,
    reverse-span-order replacement still produces the correct final
    string."""
    md = {"fields": {"contact": "Reach alice@a.com or bob@b.com please"}}
    findings = find_pii_in_metadata_for_subject(
        md, subject_identifiers=frozenset({"alice@a.com", "bob@b.com"})
    )
    assert len(findings) == 2
    full_value = findings[0].full_value
    for f in sorted(findings, key=lambda f: f.span, reverse=True):
        full_value = (
            full_value[: f.span[0]]
            + f"[REDACTED:Art.17:{f.matched_pattern}]"
            + full_value[f.span[1] :]
        )
    assert "alice@a.com" not in full_value
    assert "bob@b.com" not in full_value
    assert full_value.count("[REDACTED:Art.17:email]") == 2


# ----- Codex F23 — structured `path` tuple + jsonb_set helper -----


def test_path_tuple_to_text_array_strings() -> None:
    """Codex F23: a string-only path tuple converts to a list[str]
    with elements verbatim — the form `jsonb_set` requires."""
    assert path_to_jsonb_set_text_array(("fields", "email")) == ["fields", "email"]


def test_path_tuple_to_text_array_indices() -> None:
    """Codex F23: integer list-indices become decimal strings; string
    keys verbatim."""
    assert path_to_jsonb_set_text_array(("subjects", 1, "subject_id")) == [
        "subjects",
        "1",
        "subject_id",
    ]


def test_path_tuple_to_text_array_empty_and_root() -> None:
    """Codex F23: edge cases — empty tuple → empty list; single
    element pass-through."""
    assert path_to_jsonb_set_text_array(()) == []
    assert path_to_jsonb_set_text_array(("notes",)) == ["notes"]


def test_json_path_derived_from_path() -> None:
    """Codex F23: `json_path` is a property derived from `path` so
    the two cannot drift."""
    f1 = PiiFinding(
        path=("fields", "email"),
        matched_pattern="email",
        matched_text="a@b.c",
        span=(0, 5),
        full_value="a@b.c",
    )
    assert f1.json_path == "$.fields.email"

    f2 = PiiFinding(
        path=("subjects", 1, "role"),
        matched_pattern="name",
        matched_text="Foo Bar",
        span=(0, 7),
        full_value="Foo Bar",
    )
    assert f2.json_path == "$.subjects[1].role"

    f3 = PiiFinding(
        path=("notes",),
        matched_pattern="email",
        matched_text="a@b.c",
        span=(0, 5),
        full_value="a@b.c",
    )
    assert f3.json_path == "$.notes"


# ----- Codex F24 — numeric-string-key validator unambiguity -----


def test_path_to_jsonb_set_text_array_after_validation_is_unambiguous() -> None:
    """Codex F24: ``('fields', 0)`` (int — array index) and
    ``('fields', '0')`` (str — object key) flatten to the SAME list of
    strings — that ambiguity is exactly what the validator forbids at
    the source."""
    int_index = path_to_jsonb_set_text_array(("fields", 0))
    str_key = path_to_jsonb_set_text_array(("fields", "0"))
    assert int_index == ["fields", "0"]
    assert str_key == ["fields", "0"]
    assert int_index == str_key  # the indistinguishability F24 forbids


def test_validate_no_numeric_string_keys_accepts_clean_metadata() -> None:
    from aslan_core.timeseries.writer import _validate_no_numeric_string_keys

    _validate_no_numeric_string_keys({})
    _validate_no_numeric_string_keys({"fields": {"compensation": 1_000_000}})
    _validate_no_numeric_string_keys({"fields": ["a", "b", "c"]})
    _validate_no_numeric_string_keys({"fields": {"item_0": "x", "_1": "y"}})
    _validate_no_numeric_string_keys({"subjects": [{"subject_id": "kap-person:abc"}]})
    _validate_no_numeric_string_keys({"fields": {"nested": {"deeper": ["v1", "v2"]}}})


def test_validate_no_numeric_string_keys_rejects_top_level() -> None:
    from aslan_core.errors import MetadataSchemaViolation
    from aslan_core.timeseries.writer import _validate_no_numeric_string_keys

    with pytest.raises(MetadataSchemaViolation, match=r"[Nn]umeric"):
        _validate_no_numeric_string_keys({"0": "x"})


def test_validate_no_numeric_string_keys_rejects_nested() -> None:
    from aslan_core.errors import MetadataSchemaViolation
    from aslan_core.timeseries.writer import _validate_no_numeric_string_keys

    with pytest.raises(MetadataSchemaViolation, match=r"[Nn]umeric"):
        _validate_no_numeric_string_keys({"fields": {"01": "y"}})


def test_validate_no_numeric_string_keys_rejects_under_array() -> None:
    """Codex F24: a numeric-string key inside a dict that lives under
    an array element is still rejected."""
    from aslan_core.errors import MetadataSchemaViolation
    from aslan_core.timeseries.writer import _validate_no_numeric_string_keys

    with pytest.raises(MetadataSchemaViolation, match=r"[Nn]umeric"):
        _validate_no_numeric_string_keys({"fields": [{"42": "v"}]})


def test_validate_no_numeric_string_keys_message_cites_path() -> None:
    """Codex F24: error message names the offending path so operators
    can fix the upsert without trial-and-error."""
    import re

    from aslan_core.errors import MetadataSchemaViolation
    from aslan_core.timeseries.writer import _validate_no_numeric_string_keys

    with pytest.raises(MetadataSchemaViolation) as excinfo:
        _validate_no_numeric_string_keys({"fields": {"01": "y"}})
    assert re.search(r"fields", str(excinfo.value))
    assert re.search(r"01", str(excinfo.value))


# ----- Codex F26 — bypass-detection + Python-traversal scrubber -----


def test_has_numeric_string_keys_finds_top_level() -> None:
    from aslan_core.timeseries.pii import has_numeric_string_keys

    paths = has_numeric_string_keys({"0": "x"})
    assert paths == [("0",)]


def test_has_numeric_string_keys_finds_nested() -> None:
    from aslan_core.timeseries.pii import has_numeric_string_keys

    paths = has_numeric_string_keys({"fields": {"01": "y"}})
    assert paths == [("fields", "01")]


def test_has_numeric_string_keys_finds_nested_under_array() -> None:
    from aslan_core.timeseries.pii import has_numeric_string_keys

    paths = has_numeric_string_keys({"fields": [{"42": "v"}]})
    assert paths == [("fields", 0, "42")]


def test_has_numeric_string_keys_returns_all_paths() -> None:
    from aslan_core.timeseries.pii import has_numeric_string_keys

    paths = has_numeric_string_keys(
        {
            "0": "top-level",
            "fields": {"01": "leading-zero"},
            "buried": [{"42": "in-array"}],
        }
    )
    assert ("0",) in paths
    assert ("fields", "01") in paths
    assert ("buried", 0, "42") in paths
    assert len(paths) == 3


def test_has_numeric_string_keys_returns_empty_for_clean_metadata() -> None:
    from aslan_core.timeseries.pii import has_numeric_string_keys

    assert has_numeric_string_keys({}) == []
    assert has_numeric_string_keys(None) == []
    assert has_numeric_string_keys({"fields": {"compensation": 1}}) == []
    assert has_numeric_string_keys({"fields": ["a", "b"]}) == []
    assert has_numeric_string_keys({"fields": {"item_0": "x"}}) == []


def test_scrub_in_python_handles_numeric_string_key_at_array_index_collision() -> None:
    """Codex F26: the load-bearing F24 ambiguity case.

    metadata = ``{"fields": {"0": "alice@a.com"}, "rows": ["bob@b.com"]}``
    subject = ``bob@b.com``

    The PII scanner produces ONE finding: ``("rows", 0)`` for
    ``bob@b.com``. ``scrub_in_python`` must scrub THAT path and
    leave ``("fields", "0")`` (object key) untouched.
    """
    from aslan_core.timeseries.pii import (
        find_pii_in_metadata_for_subject,
        scrub_in_python,
    )

    md: dict[str, Any] = {"fields": {"0": "alice@a.com"}, "rows": ["bob@b.com"]}
    findings = find_pii_in_metadata_for_subject(md, subject_identifiers=frozenset({"bob@b.com"}))
    assert len(findings) == 1
    assert findings[0].path == ("rows", 0)
    assert findings[0].matched_text == "bob@b.com"

    scrubbed = scrub_in_python(md, findings)
    assert scrubbed["rows"][0] == "[REDACTED:Art.17:email]"
    assert scrubbed["fields"]["0"] == "alice@a.com"
    # Input was not mutated.
    assert md["rows"][0] == "bob@b.com"


def test_scrub_in_python_returns_input_for_empty_findings() -> None:
    """Codex F26: empty findings → returned dict equals the input but
    is a NEW object (deep copy) so callers can't accidentally share
    state."""
    from aslan_core.timeseries.pii import scrub_in_python

    md = {"fields": {"compensation": 1_000_000}}
    out = scrub_in_python(md, [])
    assert out == md
    assert out is not md
    assert out["fields"] is not md["fields"]


def test_scrub_in_python_reverse_span_order_within_shared_full_value() -> None:
    """Codex F26: when multiple findings share the same ``full_value``
    (one string carrying two PII matches), ``scrub_in_python`` applies
    replacements in REVERSE span order."""
    from aslan_core.timeseries.pii import (
        find_pii_in_metadata_for_subject,
        scrub_in_python,
    )

    md = {"fields": {"contact": "Reach alice@a.com or bob@b.com please"}}
    findings = find_pii_in_metadata_for_subject(
        md, subject_identifiers=frozenset({"alice@a.com", "bob@b.com"})
    )
    assert len(findings) == 2
    scrubbed = scrub_in_python(md, findings)
    contact = scrubbed["fields"]["contact"]
    assert "alice@a.com" not in contact
    assert "bob@b.com" not in contact
    assert contact.count("[REDACTED:Art.17:email]") == 2
