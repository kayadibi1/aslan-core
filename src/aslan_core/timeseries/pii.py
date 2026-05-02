"""PII-shape detection helpers for identifying-class series.

Codex F4 + F18 + F19 + F20 + F21 + F22 + F23 + F26.

The same regex + recursive walker is used by:

* the upsert-time guard in :class:`ObservationWriter.upsert_series`
  (primary defense — raises :class:`IdentifyingSeriesPiiInClearText` /
  :class:`IdentifyingSeriesMetadataPii`),
* the Art. 17 deletion runtime in ``aslan-service`` (defense-in-depth
  backstop — non-raising; uses the findings to drive ``jsonb_set``
  scrubs).

Both call paths MUST share this module. Drift between two copies of
the regex would silently break the F20 contract: a value scanned at
upsert time but missed by the deletion-time scanner would persist
across an erasure request.

Codex F21, 2026-04-29: ``PiiFinding`` carries ``matched_text``,
``span``, ``full_value`` so the deletion runtime can scrub the EXACT
substring (not the whole value). ``find_pii_in_metadata_for_subject``
adds subject-aware filtering so OTHER data subjects' personal data
is NOT erased when the request is for a single subject S.

Codex F22, 2026-04-29: ``_scan_string`` uses ``re.finditer`` so a
single string with N non-overlapping regex matches produces N findings
(each with its own ``span`` + ``matched_text``). Without this, a
target match appearing AFTER a non-target match in the same string
would be invisible: the subject-aware filter would drop the non-target
finding and the target would never be scrubbed. The deletion runtime,
when scrubbing multiple findings inside the same string, MUST iterate
them in REVERSE span order so earlier replacements don't shift later
spans.

Codex F23, 2026-04-29: ``PiiFinding.path`` is the structured tuple
form (``("fields", "email")`` / ``("subjects", 1, "subject_id")``)
directly convertible to the ``text[]`` argument that Postgres
``jsonb_set`` requires (via :func:`path_to_jsonb_set_text_array`).
``json_path`` is a derived read-only property for human-readable
display only.

Codex F26, 2026-04-29: :func:`has_numeric_string_keys` +
:func:`scrub_in_python` ship as a fallback path for the deletion
runtime when it encounters a row that bypassed the writer's F24 guard
(direct-DB UPDATE, manual edit, schema migration). On clean rows the
deletion runtime uses ``jsonb_set``; on bypass rows it traverses in
Python where dict-vs-list is unambiguous.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import groupby
from typing import Any, Final

# Conservative PII-shape patterns. Codex F4: false-positives are far
# less harmful than false-negatives (which would leak PII into a
# clear-text column the Art. 17 runtime cannot find).
_PII_EMAIL: Final[re.Pattern[str]] = re.compile(
    # TLD ``{1,}`` instead of ``{2,}``: short test/example TLDs like
    # ``a@b.c`` are caught. Codex F4: false-positives << false-negatives
    # — better to flag a 1-char TLD that isn't really an email than to
    # let an actual email through the upsert guard.
    r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{1,}",
    re.IGNORECASE,
)
# Two consecutive Title-Case words with optional middle initial — a
# name-shape heuristic that catches "John Doe" / "John A. Doe" /
# Turkish "İrem Yıldız". False-positives like "United States" are
# acceptable per the F4 calculus.
_PII_NAME: Final[re.Pattern[str]] = re.compile(
    r"\b[A-ZÇĞİÖŞÜ][a-zçğıöşü]+(?:\s+[A-ZÇĞİÖŞÜ]\.?)?\s+[A-ZÇĞİÖŞÜ][a-zçğıöşü]+\b"
)

_PATTERN_TABLE: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("email", _PII_EMAIL),
    ("name", _PII_NAME),
)

# Codex F19, 2026-04-29: only ``subjects`` is skipped at the top level
# (those rows round-trip through ts.series_subject + are validated at
# the SubjectRef level). ``fields`` is NOT skipped — the Art. 17
# deletion path scrubs only ``metadata.subjects[]``, so PII under
# ``metadata.fields`` would persist indefinitely.
DEFAULT_SKIP_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset({"subjects"})

# Codex F26, 2026-04-29 — deliberately duplicated from
# writer._NUMERIC_KEY_RE so the deletion-runtime helpers don't depend
# on the writer module.
_NUMERIC_STRING_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^\d+$")


@dataclass(frozen=True, slots=True)
class PiiFinding:
    """A PII-shape match in a metadata or clear-text value.

    Returned by the non-raising ``find_pii_*`` helpers so the deletion
    runtime can decide policy (scrub vs. fail vs. log).

    Codex F21, 2026-04-29 — span-based scrub contract:
    * ``matched_text`` is the exact substring that matched the regex
      (e.g. ``"john.doe@example.com"`` even if the surrounding string
      is ``"contact john.doe@example.com for details"``).
    * ``span`` is ``(start, end)`` indices into ``full_value`` so the
      deletion runtime can splice ``full_value[:start] + redaction +
      full_value[end:]`` without re-parsing.
    * ``full_value`` is the full string the match came from — equal to
      ``matched_text`` in clear-text columns where the entire field
      matched, or the surrounding string in free-form metadata.

    Codex F22, 2026-04-29 — multi-match contract:
    * A single string can carry MULTIPLE PiiFindings (one per
      non-overlapping regex match). When scrubbing several findings
      that share the same ``full_value``, apply replacements in
      REVERSE span order so earlier replacements don't shift later
      span indices.

    Codex F23, 2026-04-29 — structured-path contract:
    * ``path`` is the structured tuple form (``("fields", "email")``
      or ``("subjects", 1, "subject_id")``) directly usable by
      ``jsonb_set`` after conversion via
      :func:`path_to_jsonb_set_text_array`. Integer elements are list
      indices; string elements are dict keys.
    * ``json_path`` is a derived read-only property — DO NOT pass it
      to ``jsonb_set``; use ``path_to_jsonb_set_text_array(path)``
      instead. The two cannot drift because one is computed from the
      other.
    """

    path: tuple[str | int, ...]
    matched_pattern: str
    matched_text: str
    span: tuple[int, int]
    full_value: str

    @property
    def json_path(self) -> str:
        """Human-readable JSONPath-style display: ``"$.fields.email"``
        or ``"$.subjects[1].subject_id"``. Derived from ``path`` so
        the two cannot drift. Used in audit-log metadata; NOT used to
        drive ``jsonb_set`` (use ``path_to_jsonb_set_text_array(path)``).
        """
        parts = ["$"]
        for elem in self.path:
            if isinstance(elem, int):
                parts.append(f"[{elem}]")
            else:
                parts.append(f".{elem}")
        return "".join(parts)


def path_to_jsonb_set_text_array(path: tuple[str | int, ...]) -> list[str]:
    """Codex F23: convert a :attr:`PiiFinding.path` tuple into the
    ``text[]`` form Postgres ``jsonb_set`` requires.

    Integer indices become decimal strings; string keys are used
    verbatim (no escaping needed because Postgres treats every element
    of the array as ``text`` and dispatches to the object-key vs.
    array-index path at evaluation time).
    """
    return [str(elem) for elem in path]


def _scan_string(path: tuple[str | int, ...], full_value: str) -> Iterator[PiiFinding]:
    """Yield one :class:`PiiFinding` per non-overlapping regex match on
    ``full_value``.

    Codex F22, 2026-04-29: uses ``re.finditer`` (NOT ``re.search``) so
    adversarial inputs like ``"alice@a.com or bob@b.com"`` produce TWO
    findings (one per email). Without this, a target match that appears
    AFTER a non-target match would be invisible to
    :func:`find_pii_in_metadata_for_subject`: the filter would drop the
    non-target's finding and the target's PII would persist.

    Multiple distinct patterns may match overlapping substrings — the
    deletion runtime, when scrubbing several findings whose
    ``full_value`` is identical, should iterate them in reverse-span
    order to avoid invalidating earlier indices.
    """
    for matched_pattern, regex in _PATTERN_TABLE:
        for match in regex.finditer(full_value):
            yield PiiFinding(
                path=path,
                matched_pattern=matched_pattern,
                matched_text=match.group(0),
                span=(match.start(), match.end()),
                full_value=full_value,
            )


def find_pii_in_clear_text(field_name: str, value: str | None) -> list[PiiFinding]:
    """Scan a single clear-text column value (e.g., ``series_code``).

    Non-raising. The ``field_name`` becomes the single-element ``path``
    tuple (``(field_name,)``) so ``f.json_path`` reads as
    ``"$.<field_name>"`` and
    ``path_to_jsonb_set_text_array(f.path)`` is ``[field_name]``.
    """
    if value is None:
        return []
    return list(_scan_string((field_name,), value))


def find_pii_in_metadata(
    metadata: dict[str, Any] | None,
    *,
    skip_top_level_keys: frozenset[str] = DEFAULT_SKIP_TOP_LEVEL_KEYS,
) -> list[PiiFinding]:
    """Return ALL PII findings (no subject filter).

    Used by the upsert-time guard and by general PII sweeps. The writer
    raises on the first finding; the deletion runtime in aslan-service
    prefers :func:`find_pii_in_metadata_for_subject` so it only scrubs
    PII belonging to the target data subject.

    Codex F22: every non-overlapping regex match yields its own finding
    (so adversarial multi-match strings don't hide later matches behind
    earlier ones).

    Numeric / boolean / None values are skipped — the regexes only
    apply to strings.
    """
    findings: list[PiiFinding] = []
    if not metadata:
        return findings
    for key, value in metadata.items():
        if key in skip_top_level_keys:
            continue
        _walk((key,), value, findings)
    return findings


def _nfc_lower(s: str) -> str:
    """NFC-normalize and casefold for substring containment checks (F21)."""
    return unicodedata.normalize("NFC", s).casefold()


def find_pii_in_metadata_for_subject(
    metadata: dict[str, Any] | None,
    *,
    subject_identifiers: frozenset[str],
    skip_top_level_keys: frozenset[str] = DEFAULT_SKIP_TOP_LEVEL_KEYS,
) -> list[PiiFinding]:
    """Return only findings whose ``matched_text`` equals or contains
    any of the target data subject's known identifiers
    (case-insensitive, NFC-normalized substring match).

    For Art. 17 scrubbing. Non-matching PII findings are filtered out —
    they belong to OTHER data subjects and must not be erased on this
    subject's request. Operators wanting a general PII sweep should use
    :func:`find_pii_in_metadata` and apply their own policy.

    ``subject_identifiers`` is the operator-resolved set of
    natural-person identifiers for the target subject S: ``{S}`` plus
    any aliases (email addresses, full names, phone numbers, free-text
    IDs from ``ts.series_subject``'s role-specific metadata). Empty
    set → empty list (nothing to scrub).
    """
    if not subject_identifiers:
        return []
    all_findings = find_pii_in_metadata(metadata, skip_top_level_keys=skip_top_level_keys)
    needles = tuple(_nfc_lower(s) for s in subject_identifiers if s)
    if not needles:
        return []
    matched: list[PiiFinding] = []
    for f in all_findings:
        haystack = _nfc_lower(f.matched_text)
        if any(n in haystack or haystack in n for n in needles):
            matched.append(f)
    return matched


def _walk(path: tuple[str | int, ...], value: Any, out: list[PiiFinding]) -> None:
    """Recursive walker. ``path`` accumulates as a tuple so each leaf
    receives a structured ``path`` directly usable by
    :func:`path_to_jsonb_set_text_array` (codex F23). String elements
    are dict keys; integer elements are list indices.
    """
    if isinstance(value, str):
        out.extend(_scan_string(path, value))
    elif isinstance(value, dict):
        for k, v in value.items():
            _walk((*path, k), v, out)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _walk((*path, i), v, out)
    # ints / floats / bools / None: nothing to scan.


# Codex F26, 2026-04-29 — bypass-detection helpers for the Art. 17
# deletion runtime. F24's writer guard makes
# `path_to_jsonb_set_text_array` lossless for rows the writer touched.
# But the deletion runtime in `aslan-service` ALSO has to handle rows
# that landed via direct-DB UPDATEs, manual edits, or schema migrations
# — those rows can contain numeric-string object keys that would make
# `jsonb_set` ambiguous. The helpers below detect such rows and switch
# the deletion runtime to a Python-traversal scrub path that preserves
# dict-vs-list at every step.


def has_numeric_string_keys(
    metadata: dict[str, Any] | None,
) -> list[tuple[str | int, ...]]:
    """Codex F26: return paths to any numeric-string object keys in
    ``metadata``.

    Empty list = clean row (passed the writer's
    ``_validate_no_numeric_string_keys`` guard) — the deletion runtime
    can use ``jsonb_set`` unambiguously.

    Non-empty list = legacy/bypass row — the deletion runtime must use
    :func:`scrub_in_python` instead and emit a
    ``series.metadata_bypass_detected`` audit event. Each path is a
    structured ``tuple[str | int, ...]`` (same form as
    ``PiiFinding.path``) so operators can locate the offending keys.

    Walks dicts and lists/tuples recursively. Numeric-string keys
    inside lists count: ``{"fields": [{"42": "x"}]}`` returns
    ``[("fields", 0, "42")]``.
    """
    if not metadata:
        return []
    out: list[tuple[str | int, ...]] = []
    _walk_for_numeric_keys((), metadata, out)
    return out


def _walk_for_numeric_keys(
    path: tuple[str | int, ...],
    obj: Any,
    out: list[tuple[str | int, ...]],
) -> None:
    """Codex F26: recursive walker mirroring the writer's
    ``_validate_no_numeric_string_keys`` but non-raising — appends each
    offending path to ``out`` so the caller can emit a single audit
    event listing all of them.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and _NUMERIC_STRING_KEY_RE.match(k):
                out.append((*path, k))
            _walk_for_numeric_keys((*path, k), v, out)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _walk_for_numeric_keys((*path, i), v, out)


def scrub_in_python(
    metadata: dict[str, Any],
    findings: list[PiiFinding],
) -> dict[str, Any]:
    """Codex F26: apply replacements to ``metadata`` using each
    finding's structured ``path`` and reverse-span order within each
    shared ``full_value``, returning a NEW dict (deep-copied).

    Used by the Art. 17 deletion runtime as the fallback when
    :func:`has_numeric_string_keys` returns a non-empty list
    (legacy/bypass row). Preserves the dict-vs-list distinction
    unambiguously because traversal happens in Python where the
    container kind is known by inspection at each step.

    Replacement format mirrors the ``jsonb_set`` path:
    ``f"[REDACTED:Art.17:{matched_pattern}]"``. Findings sharing a
    ``path`` are grouped and applied in reverse-span order so earlier
    (left-of-cursor) replacements don't shift later span indices.

    Empty findings list → input is deep-copied and returned unchanged
    so the contract — "returns a NEW dict" — holds even on no-op calls.
    """
    out = copy.deepcopy(metadata)
    if not findings:
        return out

    # Group findings by their path so each shared `full_value` is
    # replaced once with all its non-overlapping matches applied in
    # reverse-span order (mirrors spec §5 step 2c jsonb_set loop).
    sorted_findings = sorted(findings, key=lambda f: (f.path, f.span))
    for path_tuple, group_iter in groupby(sorted_findings, key=lambda f: f.path):
        group_list = list(group_iter)
        full_value = group_list[0].full_value
        for f in sorted(group_list, key=lambda f: f.span, reverse=True):
            full_value = (
                full_value[: f.span[0]]
                + f"[REDACTED:Art.17:{f.matched_pattern}]"
                + full_value[f.span[1] :]
            )
        _set_at_path(out, path_tuple, full_value)
    return out


def _set_at_path(
    container: Any,
    path: tuple[str | int, ...],
    new_value: Any,
) -> None:
    """Codex F26: in-place set ``container[path[0]][path[1]]...`` to
    ``new_value``. ``path`` mixes ``str`` (dict keys) and ``int`` (list
    indices); the walker dispatches by element type. Raises
    :class:`KeyError` or :class:`IndexError` if the path doesn't
    resolve — the caller should have derived ``path`` from a
    :class:`PiiFinding` produced by scanning the same metadata, so this
    is an internal-consistency assertion.
    """
    if not path:
        return
    cursor: Any = container
    for elem in path[:-1]:
        cursor = cursor[elem]
    cursor[path[-1]] = new_value


__all__ = [
    "DEFAULT_SKIP_TOP_LEVEL_KEYS",
    "PiiFinding",
    "find_pii_in_clear_text",
    "find_pii_in_metadata",
    "find_pii_in_metadata_for_subject",
    "has_numeric_string_keys",
    "path_to_jsonb_set_text_array",
    "scrub_in_python",
]
