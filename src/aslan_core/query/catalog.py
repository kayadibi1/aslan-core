"""Catalog query functions for canonical financial line items.

Provides:
- load_canonical_lines: parse a canonical_lines.yaml manifest into CanonicalLineInfo objects
- list_canonical_lines: filter a loaded list by statement type
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from aslan_core.query.schemas import CanonicalLineInfo, StatementType

__all__ = [
    "list_canonical_lines",
    "load_canonical_lines",
]


def load_canonical_lines(yaml_path: Path) -> list[CanonicalLineInfo]:
    """Load canonical line metadata from a YAML manifest.

    Reads the ``lines`` mapping from the manifest and returns one
    :class:`~aslan_core.query.schemas.CanonicalLineInfo` per entry.
    Fields not present in ``CanonicalLineInfo`` (e.g. ``sources``,
    ``formula``, ``sign_convention``) are silently ignored.

    Args:
        yaml_path: Absolute or relative path to the ``canonical_lines.yaml``
            manifest.

    Returns:
        List of :class:`~aslan_core.query.schemas.CanonicalLineInfo` objects,
        one per entry in the manifest's ``lines`` mapping, in manifest order.

    Raises:
        FileNotFoundError: If ``yaml_path`` does not exist.
        ValueError: If the manifest is missing the ``lines`` key or an entry
            is missing required fields.
    """
    raw: str = yaml_path.read_text(encoding="utf-8")
    doc: dict[str, Any] = yaml.safe_load(raw)

    lines_map: dict[str, Any] | None = doc.get("lines")
    if lines_map is None:
        raise ValueError(f"canonical_lines manifest at {yaml_path} has no 'lines' key")

    result: list[CanonicalLineInfo] = []
    for canonical_code, entry in lines_map.items():
        result.append(
            CanonicalLineInfo(
                canonical_code=canonical_code,
                statement_type=StatementType(entry["statement_type"]),
                description=entry["description"],
                computed=bool(entry["computed"]),
                required=bool(entry["required"]),
                monetary=bool(entry["monetary"]),
            )
        )
    return result


def list_canonical_lines(
    lines: list[CanonicalLineInfo],
    statement_type: StatementType | None = None,
) -> list[CanonicalLineInfo]:
    """Filter a loaded list of canonical lines by statement type.

    Args:
        lines: Pre-loaded list returned by :func:`load_canonical_lines`.
        statement_type: When provided, only lines whose
            :attr:`~aslan_core.query.schemas.CanonicalLineInfo.statement_type`
            matches are returned.  When ``None``, all lines are returned.

    Returns:
        A new list (never a view) containing only the matching entries.
    """
    if statement_type is None:
        return list(lines)
    return [li for li in lines if li.statement_type == statement_type]
