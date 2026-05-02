"""Type-safety floor: every dashboard view-model field's annotation is
in a safe allowlist.

Spec §8.1 + codex round-4: ``extra='forbid'`` only blocks unknown KEYS,
not unsafe TYPES of allowed fields. A future field typed as ``Any``,
``object``, ``dict[Any, Any]``, ``BaseException``, or a SQLAlchemy
``Row`` could leak through implicit ``__str__`` / ``__repr__``.

The allowlist:

  Primitives: ``str``, ``int``, ``float``, ``bool``, ``datetime``,
  ``UUID``, ``Decimal``, ``type(None)``.

  Closed enums: ``Literal[...]``, ``Enum`` subclasses.

  Containers: ``list[T]``, ``tuple[T, ...]``, ``dict[str, T]`` — with
  ``T`` recursively in the allowlist (``dict`` keys MUST be ``str``).

  Unions: ``T | None`` and ``Union[T, ...]`` — every member in the
  allowlist.

  Nested VMs: any ``BaseModel`` subclass whose own fields all pass
  (recursive).

Forbidden tokens: ``Any``, ``object``, ``BaseException``, ``Exception``,
``dict`` without parameterisation, ``tuple`` without parameterisation,
anything from ``sqlalchemy.engine``. ``bytes`` is rejected — base64-
encode at the query layer if a binary blob ever needs to render.
"""

from __future__ import annotations

import inspect
import types
import typing
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel

_SAFE_PRIMITIVES: frozenset[type] = frozenset(
    {str, int, float, bool, datetime, UUID, Decimal, type(None)}
)
_FORBIDDEN_TYPES: frozenset[object] = frozenset({Any, object, BaseException, Exception, bytes})


def _is_union(origin: object) -> bool:
    return origin is Union or origin is types.UnionType


def _is_safe_type(tp: Any) -> bool:
    if tp in _FORBIDDEN_TYPES:
        return False
    if tp in _SAFE_PRIMITIVES:
        return True
    origin = get_origin(tp)
    if origin is Literal:
        return True
    if _is_union(origin):
        return all(_is_safe_type(arg) for arg in get_args(tp))
    if isinstance(tp, type):
        if issubclass(tp, Enum):
            return True
        if issubclass(tp, BaseModel):
            return all(_is_safe_type(f.annotation) for f in tp.model_fields.values())
    if origin in (list, tuple):
        return all(_is_safe_type(arg) for arg in get_args(tp))
    if origin is dict:
        args = get_args(tp)
        return len(args) == 2 and args[0] is str and _is_safe_type(args[1])
    return False


def test_every_vm_has_safe_field_types() -> None:
    from aslan_core.dashboard import view_models

    vms: list[type[BaseModel]] = [
        cls
        for _, cls in inspect.getmembers(view_models, inspect.isclass)
        if issubclass(cls, BaseModel) and cls is not BaseModel and not cls.__name__.startswith("_")
    ]
    assert vms, "view_models.py must export at least one VM"
    for vm in vms:
        for field_name, field in vm.model_fields.items():
            assert _is_safe_type(field.annotation), (
                f"{vm.__name__}.{field_name}: {field.annotation!r} is not in the allowlist"
            )


def test_forbidden_types_are_caught() -> None:
    """Sanity: the allowlist actually rejects the forbidden tokens.
    Catches a future change that accidentally permits ``Any``."""
    assert not _is_safe_type(Any)
    assert not _is_safe_type(object)
    assert not _is_safe_type(bytes)
    assert not _is_safe_type(BaseException)
    assert not _is_safe_type(dict)  # unparameterised
    assert not _is_safe_type(dict[int, str])  # non-str key
    assert _is_safe_type(int)
    assert _is_safe_type(str | None)
    assert _is_safe_type(list[int])
    assert _is_safe_type(dict[str, int])
    assert _is_safe_type(Literal["a", "b"])


_ = typing  # quiet unused-import lint when typing only feeds annotations
