"""Required checks have one source: the GitHub ruleset of the base branch (`repos/R/rules/branches/main`), read per
repository when a pull request is judged. sm keeps no list of check names of its own (no `coverage-floor`, no Sonar
name): a check the rules require holds a pull request until it is green, every other check informs and never holds.

A repository whose rules require no check at all (a private repository without GitHub Pro has no rules) is judged on all
its checks but the advisory review, and so is one whose rules cannot be read: never a looser gate than GitHub's own."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# The Claude review runs as `review`; its inner job `attempt` (heinzel #454) can time out or be cancelled. Advisory only
# always: the review informs, it never holds.
ADVISORY_REVIEW = frozenset({"review", "attempt", "Claude review"})


def required_in_order(rules: Any) -> list[str] | None:
    """The contexts of every required_status_checks rule of a `rules/branches/BRANCH` answer, in the rules' order; None
    when it is no answer."""
    if not isinstance(rules, list):
        return None
    try:
        names = [str(check["context"]) for rule in rules if isinstance(rule, dict) and rule.get("type") == "required_status_checks"
                 for check in (rule.get("parameters") or {})["required_status_checks"]]
    except (KeyError, TypeError):
        return None
    return list(dict.fromkeys(names))


def required_from(rules: Any) -> frozenset[str] | None:
    names = required_in_order(rules)
    return None if names is None else frozenset(names)


@dataclass(frozen=True)
class Advisory:
    """The checks that do not hold a pull request, as a predicate: `name in advisory`: the advisory review and `waived`
    names, the same with or without a ruleset (`awaited` names are waited for anyway). `advisory | names` waives names,
    `advisory - names` awaits them. The rules only say which checks must have reported (`missing`)."""

    required: frozenset[str] | None
    waived: frozenset[str] = field(default_factory=frozenset)
    awaited: frozenset[str] = field(default_factory=frozenset)

    @property
    def ruled(self) -> bool:
        """The rules were read and require at least one check: they alone decide."""
        return bool(self.required)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str) or name in self.awaited:
            return False
        return name in ADVISORY_REVIEW or name in self.waived

    def __or__(self, other: Iterable[str]) -> Advisory:
        return Advisory(self.required, self.waived | frozenset(other), self.awaited)

    def __sub__(self, other: Iterable[str]) -> Advisory:
        return Advisory(self.required, self.waived, self.awaited | frozenset(other))

    def names(self) -> list[str]:
        """The required contexts, sorted ([] without a ruleset that requires any)."""
        return sorted(self.required or ())

    def missing(self, rollup: list[dict[str, Any]]) -> list[str]:
        """Required contexts with no entry at all in a PR's status rollup: GitHub holds the merge until they report."""
        present = {str(c.get("name") or c.get("context") or "") for c in rollup}
        return [name for name in self.names() if name not in present]


def of_rules(rules: Any) -> Advisory:
    return Advisory(required_from(rules))
