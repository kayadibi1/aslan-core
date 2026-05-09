"""Rule protocol shared by dq.validation and per-table rule modules.

`RuleResult` lives here (rather than in `dq.validation`) so per-table
rule modules can import it without forming a cycle with
`dq.validation` (which in turn imports the rule modules to populate
`_DEFAULT_RULES`).

Internal module; consumers import `Rule`, `RuleFn`, and `RuleResult`
via `aslan_core.dq.rules` (the package re-exports all three) or
`aslan_core.dq.validation` (which re-exports `RuleResult`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aslan_core.dq.types import Severity


@dataclass(frozen=True, slots=True)
class RuleResult:
    """What a rule callable returns for a single firing.

    Distinct from `ValidationFailure` (the persisted shape) because
    a rule does not know its own source/record_table/record_pk —
    those are supplied by the surrounding `validation.check()` call.
    """

    rule_name: str
    severity: Severity
    detail: dict[str, Any]


# A rule callable: takes a record dict, returns a list of RuleResult.
RuleFn = Callable[[dict[str, Any]], list[RuleResult]]


@dataclass(frozen=True, slots=True)
class Rule:
    """One validation rule registered against one table."""

    name: str
    run: RuleFn
