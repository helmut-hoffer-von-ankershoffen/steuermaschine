"""The repository identity the admission is bound to, and the PR key shape of the PR actions file.

`CORE` is a reviewed constant, on purpose not a setting: an admission needs three values to agree (this constant, the
origin of the checkout, the repository literal in trusted main), and only this one is out of the operator's hands.
The moved code refers to this module as `hzlib`, the name it had, so its lines stay as they were. The values and
`split_key` are those of the former module of that name (pinned by tests/test_native_admission_ops.py).
"""

from __future__ import annotations

OWNER = "helmut-hoffer-von-ankershoffen"
CORE = f"{OWNER}/heinzel"
PLUGINS = f"{OWNER}/heinzel-plugins"
SHORT = {"": CORE, "core": CORE, "plugins": PLUGINS, "acme": f"{OWNER}/heinzel-acme", "sf": f"{OWNER}/heinzel-sf"}


def split_key(tokens: list[str] | str) -> tuple[str, str, str]:
    """`393`, `#393`, `plugins 38`, `plugins #38` -> (repo, number, key as the PR actions file writes it)."""
    words = tokens.split() if isinstance(tokens, str) else list(tokens)
    prefix = words[0] if len(words) > 1 else ""
    number = words[-1].lstrip("#")
    if prefix not in SHORT or not number.isdigit():
        raise ValueError(f"not a PR: {' '.join(words)}")
    return SHORT[prefix], number, f"{prefix} #{number}" if prefix else number
