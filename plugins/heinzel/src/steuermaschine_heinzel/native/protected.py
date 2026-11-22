"""The paths whose change needs a recorded review of exactly that head, and the digest that review approves.

Shared by `sm head admit` (it stops for the verdict) and the queue's merge rule (it refuses to merge without it), so both
name the same paths and the same bytes. No plan registry and no copy of Core's own lists: the execution inputs Core names
are read from Core at main by the caller (its PROTECTED_FILES), while Core still has them.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import tomllib
from typing import Any

PROTECTED = (".github/*", "release.sh", "*/release.sh", "scripts/*release*", "scripts/*publish*", "scripts/*pypi*", "scripts/*acceptance*",
             "scripts/*gate*", "scripts/focused_*", "scripts/ci_workload.py", "scripts/release_candidate.py", "scripts/release_policy.json",
             "quarantine.toml", "*/quarantine.toml", "conftest.py", "*/conftest.py", ".coveragerc", "*/.coveragerc", "scripts/*coverage*",
             "Makefile", "uv.lock", ".pre-commit-config.yaml", "sonar-project.properties")
TOOL_TABLES = ("coverage", "pytest")  # the coverage config and the pytest options: they lower a floor, deselect tests or raise a timeout
EXECUTION_INPUTS = "PROTECTED_FILES"  # Core's own list of the files whose change alters what CI runs (scripts/focused_plan.py)


def matches(names: list[str], extra: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """The names that are protected by pattern or named in `extra` (Core's execution inputs)."""
    return {n for n in names if n in extra or any(fnmatch.fnmatchcase(n, pat) for pat in PROTECTED)}


def execution_inputs(plan_text: str | None) -> set[str]:
    """Core's own list of the files whose change alters what CI runs: `PROTECTED_FILES`, a literal tuple in its
    scripts/focused_plan.py at main. No file (Core dropped it): empty. A file whose list is missing or not a literal:
    ValueError, so the caller refuses; an unreadable list never means "nothing protected"."""
    if plan_text is None:
        return set()
    try:
        for node in ast.parse(plan_text).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == EXECUTION_INPUTS for t in node.targets):
                value: Any = ast.literal_eval(node.value)
                if isinstance(value, (tuple, list)) and value and all(isinstance(v, str) for v in value):
                    return set(value)
                break
    except (SyntaxError, ValueError, TypeError):
        pass
    raise ValueError(f"scripts/focused_plan.py is there but its {EXECUTION_INPUTS} is missing or not a literal list of paths")


def tool_tables(text: str) -> dict[str, Any]:
    """The `tool.coverage` and `tool.pytest` tables of a pyproject.toml text; ValueError when it is not valid TOML."""
    try:
        tool = tomllib.loads(text).get("tool", {})
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"pyproject.toml is not valid TOML: {exc}") from None
    return {name: tool.get(name) for name in TOOL_TABLES}


def pyproject_weakens(base_text: str | None, head_text: str | None) -> bool:
    """True when the coverage or pytest tables differ, or when either side is missing or cannot be parsed (fail closed)."""
    if base_text is None or head_text is None:
        return True
    try:
        return tool_tables(base_text) != tool_tables(head_text)
    except ValueError:
        return True


def record(blobs: dict[str, str]) -> tuple[str, str]:
    """(digest, text) of the review record: each protected path with the id of its content at the head (`removed` when the head
    deletes it). The digest follows the content; the verdict is recorded for the commit (`sm review verdict --head`): a new head needs a new verdict."""
    text = json.dumps({"protected": dict(sorted(blobs.items()))}, indent=1, sort_keys=True) + "\n"
    return hashlib.sha256(text.encode()).hexdigest(), text
